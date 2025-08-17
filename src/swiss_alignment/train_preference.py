import logging
import math
from datetime import timedelta
from pathlib import Path

import accelerate
import datasets
import hydra
import numpy as np
import torch
import wandb
from accelerate.logging import get_logger
from accelerate.state import PartialState
from datasets import DatasetDict
from omegaconf import DictConfig, OmegaConf
from transformers import AutoTokenizer
from trl import (
    ModelConfig,
    ScriptArguments,
    get_kbit_device_map,
    get_peft_config,
    get_quantization_config,
)

from swiss_alignment import utils
from swiss_alignment.data_sft.utils_for_dataset import load_dataset_flexible
from swiss_alignment.trainers.preference import (
    PreferenceTrainer,
    PreferenceTrainerConfig,
)
from swiss_alignment.utils import utils_for_trl

utils.config.register_resolvers()
acc_state = PartialState(
    **accelerate.InitProcessGroupKwargs(timeout=timedelta(hours=4)).to_kwargs()
)
acc_logger = get_logger(__name__)
hydra_logger = logging.getLogger(__name__)


@hydra.main(version_base=None, config_path="configs", config_name="train-preference")
def main(config: DictConfig) -> None:
    ############################ Config Setup ############################

    config = utils_for_trl.setup_config_and_resuming(config, acc_state, acc_logger)
    # full_config is a merge with the TRL arg dataclasses
    # The args dataclasses are used by the HF classes, and the full_config by the template.
    script_args = ScriptArguments(**OmegaConf.to_container(config.script_args))
    training_args = PreferenceTrainerConfig(
        **OmegaConf.to_container(config.training_args), output_dir=str(Path.cwd())
    )
    model_args = ModelConfig(**OmegaConf.to_container(config.model_args))

    quantization_config = get_quantization_config(model_args)
    model_kwargs = dict(
        revision=model_args.model_revision,
        trust_remote_code=model_args.trust_remote_code,
        attn_implementation=model_args.attn_implementation,
        torch_dtype=model_args.torch_dtype,
        use_cache=False,
        device_map=get_kbit_device_map() if quantization_config is not None else None,
        quantization_config=quantization_config,
    )
    training_args.model_init_kwargs = model_kwargs
    if (
        training_args.ref_logprobs_from_dataset
        or training_args.pre_compute_ref_logprobs
    ):
        ref_model = None
    else:
        ref_model = model_args.model_name_or_path
        training_args.ref_model_init_kwargs = model_kwargs

    full_config = utils_for_trl.merge_and_save_config(
        config, script_args, training_args, model_args, acc_state
    )
    if acc_state.is_main_process:
        utils.config.setup_wandb(full_config, acc_logger)
        utils.config.try_sync_wandb()
    utils.seeding.seed_everything(config)

    ############################ Tokenizer Setup ############################

    tokenizer = AutoTokenizer.from_pretrained(
        model_args.model_name_or_path, trust_remote_code=model_args.trust_remote_code
    )
    # Perform checks
    if tokenizer.pad_token is None:
        acc_logger.warning(
            f"Tokenizer does not have a pad token. Setting it to config.tokenizer_args.pad_token_id = {config.tokenizer_args.pad_token_id}"
        )
        tokenizer.pad_token_id = config.tokenizer_args.pad_token_id
    if tokenizer.pad_token == tokenizer.eos_token:
        raise ValueError(
            "Tokenizer pad token is the same as the eos token. The eos will be masked as if it was a pad."
        )

    ############################ Dataset Setup ############################

    with acc_state.main_process_first():
        ds = load_dataset_flexible(config.script_args.dataset_name)

        if isinstance(ds, DatasetDict):
            ds = DatasetDict(
                {
                    "train": ds[config.script_args.dataset_train_split],
                    **(
                        {"eval": ds[config.script_args.dataset_test_split]}
                        if config.script_args.dataset_test_split is not None
                        else {}
                    ),
                }
            )
        elif isinstance(ds, datasets.Dataset):
            # Convert Dataset to DatasetDict with train split
            ds = DatasetDict(
                {
                    "train": ds,
                }
            )
        for split_name in ds.keys():
            if config.dataset_args.debug_subsample[split_name] > 0:
                ds[split_name] = ds[split_name].select(
                    range(
                        min(
                            len(ds[split_name]),
                            config.dataset_args.debug_subsample[split_name],
                        )
                    )
                )
        if config.dataset_args.debug_oom:
            if "max_chosen_rejected_reward_tokens_len" in ds["train"].column_names:
                ds = ds.sort("max_chosen_rejected_reward_tokens_len", reverse=True)
            elif "max_chosen_rejected_model_tokens_len" in ds["train"].column_names:
                ds = ds.sort("max_chosen_rejected_model_tokens_len", reverse=True)
            else:
                acc_logger.warning(
                    "No column for sorting dataset found. Using default order."
                )

        # Shuffle at the end to preserve previous cache across seeds.
        ds = ds.shuffle(seed=config.seed)

        # We only support the "preference with implicit prompt" format
        # with "chosen" and "rejected" columns including both chat and ref completions
        # https://huggingface.co/docs/trl/main/en/dataset_formats#preference
        # Drop the extra preference columns
        for extra_key in ["prompt", "completion", "messages", "label"]:
            if extra_key in ds[split_name].column_names:
                ds[split_name] = ds[split_name].remove_columns([extra_key])

        if "ref_completions" in ds[split_name].column_names:
            ds[split_name] = ds[split_name].remove_columns(["ref_completions"])

    ############################ Trainer Setup ############################

    # Find the last checkpoint
    resuming_dir = Path.cwd()
    # Handle resuming
    last_checkpoint_number = 0
    for item in resuming_dir.iterdir():
        if item.is_dir() and item.name.startswith("checkpoint-"):
            if (item / "scheduler.pt").is_file() and (
                item / "trainer_state.json"
            ).is_file():
                last_checkpoint_number = max(
                    last_checkpoint_number, int(item.name.split("-")[-1])
                )

    if last_checkpoint_number > 0:
        acc_logger.info(
            f"TRL will attempt to resume from last checkpoint: {last_checkpoint_number}"
        )
        eval_file = resuming_dir / f"eval_{last_checkpoint_number}_results.json"
        if eval_file.exists():
            training_args.eval_on_start = False

    trainer = PreferenceTrainer(
        model_args.model_name_or_path,
        ref_model=ref_model,
        args=training_args,
        train_dataset=ds["train"],
        eval_dataset=ds["eval"] if training_args.eval_strategy != "no" else None,
        processing_class=tokenizer,
        peft_config=get_peft_config(model_args),
    )

    # Computing the warmup steps for beta3 and alpha in AdEMAMix
    if training_args.optim == "ademamix":
        len_ds = len(ds["train"])
        total_batch_size = trainer.get_total_train_batch_size(training_args)
        num_steps_per_epoch = int(
            len_ds // total_batch_size
            if training_args.dataloader_drop_last
            else math.ceil(len_ds / total_batch_size)
        )
        total_steps = training_args.num_train_epochs * num_steps_per_epoch
        # TODO move the beta3 and alpha to the training_args.optim_args command line argument.
        # This is not trivial for write in a way that is sent in a correct format through all the layers down to hydra.
        training_args.optim_args = (
            f"beta3=0.999,alpha=8.0,t_beta3={total_steps},t_alpha={total_steps}"
        )
        acc_logger.info(f"AdEMAMix optim_args: {trainer.args.optim_args}")

    trainer.train(resume_from_checkpoint=last_checkpoint_number > 0)
    acc_logger.info("Training completed. Performing final evaluation.")

    last_eval_file = resuming_dir / f"eval_results.json"
    if training_args.eval_strategy != "no":
        if last_eval_file.exists():
            acc_logger.info("Last evaluation already performed.")
        else:
            torch.cuda.empty_cache()
            acc_logger.info("Performing final evaluation.")
            metrics = trainer.evaluate()
            trainer.log_metrics("eval", metrics)
            trainer.save_metrics("eval", metrics)
            acc_logger.info("Final evaluation completed.")

    acc_state.wait_for_everyone()
    acc_logger.info("Training completed. Checkpoints saved.")
    if acc_state.is_main_process:
        wandb.finish()
        utils.config.try_sync_wandb()
    acc_state.wait_for_everyone()
    accelerate.Accelerator().end_training()


if __name__ == "__main__":
    main()
