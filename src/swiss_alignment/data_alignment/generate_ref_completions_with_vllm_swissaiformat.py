# import json
# import logging
# import math
# import os
# import random
# from pathlib import Path
#
# import datasets
# import hydra
# from omegaconf import DictConfig
# from tqdm import tqdm
# from transformers import AutoTokenizer
# from vllm import LLM, SamplingParams
#
# from swiss_alignment import utils
#
# utils.config.register_resolvers()
# logger = logging.getLogger(__name__)
#
#
# # Prepare a function to generate completions in batches
# def generate_completions_batch(llm, batch, tokenizer, config):
#     # Prepare prompts
#     tokenized_prompts = [
#         tokenizer.apply_chat_template(
#             [sample[0]], tokenize=True, add_generation_prompt=True,
#         )
#         for sample in batch
#     ]
#     # Add prompt length
#     lengths = [len(tokenized_prompt) for tokenized_prompt in tokenized_prompts]
#
#     # Add the prompt length to the dataset.
#     def add_chat_num_tokens(row):
#
#         return {
#             "generation_prompt_model_tokens_len":
#         }
#
#     data = data.map(add_chat_num_tokens, num_proc=256)
#
#     # Filter out the prompts that are too long
#
#     mask_prompt = [length >= config.max_prompt_length for length in lengths]
#     tokenized_prompts = [tokenized_prompt[:config.max_prompt_length-1] for tokenized_prompt in tokenized_prompts]
#
#     sampling_params = SamplingParams(
#         temperature=config.model_generation_config.temperature,
#         top_p=config.model_generation_config.top_p,
#         n=config.n_completions,
#         max_tokens=config.max_new_tokens,
#     )
#     # Generate completions
#     decoded_prompts = [tokenizer.decode(tokenized_prompt, skip_special_tokens=False) for tokenized_prompt in tokenized_prompts]
#     outputs = llm.generate(decoded_prompts, sampling_params)
#
#     results = []
#     for i, output in enumerate(outputs):
#         completions = [completion.text.strip() for completion in output.outputs]
#         results.append(
#             [
#                 {
#                     "role": "user",
#                     "content": batch[i][0]["content"],
#                 },
#                 {
#                     "role": "assistant",
#                     "content": json.dumps(completions),
#                 },  # Serialized list of completions.
#             ]
#         )
#
#     return results
#
#
# def compute_subpartition_start_end_indices(
#     partition_start_idx, partition_end_idx, subpartition_number, num_subpartitions
# ):
#     subpartition_size = math.ceil((partition_end_idx - partition_start_idx) / num_subpartitions)
#     start_idx = partition_start_idx + subpartition_number * subpartition_size
#     end_idx = partition_start_idx + (subpartition_number + 1) * subpartition_size
#     end_idx = min(end_idx, partition_end_idx)
#
#     return start_idx, end_idx
#
#
# @hydra.main(
#     version_base=None, config_path="../configs", config_name="generate-ref-completions"
# )
# def main(config: DictConfig) -> None:
#     config = utils.config.setup_config_and_resuming(config)
#     random.seed(config.seed)
#
#     tp_size = config.model_vllm_config.tensor_parallel_size
#     cuda_devices = [config.subpartition_number * tp_size + i for i in range(tp_size)]
#     os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(map(str, cuda_devices))
#     logger.info(f"Using GPUs: {os.environ['CUDA_VISIBLE_DEVICES']}")
#
#     # Model
#     llm = LLM(
#         model=config.model_args.model_name_or_path,
#         dtype=config.model_args.torch_dtype,
#         model_impl=config.model_vllm_config.model_impl,
#         tensor_parallel_size=config.model_vllm_config.tensor_parallel_size,
#     )
#     tokenizer = AutoTokenizer.from_pretrained(config.model_args.model_name_or_path)
#
#     # End index is exclusive
#     num_subpartitions = config.num_gpus_per_node // config.model_vllm_config.tensor_parallel_size
#     (
#         subpartition_start_idx,
#         subpartition_end_idx,
#     ) = compute_subpartition_start_end_indices(
#         config.partition_start_idx, config.partition_end_idx, config.subpartition_number, num_subpartitions
#     )
#
#     if subpartition_start_idx >= subpartition_end_idx:
#         logger.info("Subpartition is empty. Exiting.")
#         return
#
#     subpartition_data = datasets.load_from_disk(config.dataset_args.dataset_name)[
#         config.split
#     ].select(range(subpartition_start_idx, subpartition_end_idx))
#
#     # Handle resuming.
#     resuming_dir = Path.cwd()
#     # Checkpoints are saved as `checkpoint-{last-relative-index-processed-in-the-subpartition}`.
#     already_processed_samples = max(
#         (
#             int(item.name.split("-")[-1])
#             for item in resuming_dir.iterdir()
#             if item.is_dir() and item.name.startswith("checkpoint-")
#         ),
#         default=0,
#     )
#     if already_processed_samples == len(subpartition_data):
#         logger.info(
#             "All samples in the subpartition have already been processed. Exiting."
#         )
#         return
#
#     local_start_idx = already_processed_samples  # 64, 128, ...
#     if local_start_idx > 0:
#         logger.info(
#             f"Resuming from checkpoint-{local_start_idx}. Processing from sample {local_start_idx}."
#         )
#
#     pbar = tqdm(total=len(subpartition_data), desc="Generating completions")
#     pbar.update(local_start_idx)
#     while local_start_idx < len(subpartition_data):
#         current_slice = (
#             local_start_idx,
#             min(local_start_idx + config.save_interval, len(subpartition_data)),
#         )
#         current_slice_data = subpartition_data.select(range(*current_slice))
#         local_end_idx = local_start_idx + len(current_slice_data)
#
#         processed_chunk = generate_completions_batch(
#             llm, current_slice_data, tokenizer, config
#         )
#
#         current_slice_data = subpartition_data.select(range(*current_slice))
#         current_slice_data = current_slice_data.map(
#             lambda _, idx: {"ref_completions": processed_chunk[idx]}, with_indices=True
#         )
#         save_path = resuming_dir / f"checkpoint-{local_end_idx}"
#         current_slice_data.save_to_disk(save_path)
#         logger.info(f"Saved checkpoint-{local_end_idx} successfully!")
#
#         pbar.update(len(current_slice_data))
#
#         local_start_idx = local_end_idx  # Update start index for the next chunk
#
#     logger.info("Completions generated and saved successfully!")
#
#
# if __name__ == "__main__":
#     main()
