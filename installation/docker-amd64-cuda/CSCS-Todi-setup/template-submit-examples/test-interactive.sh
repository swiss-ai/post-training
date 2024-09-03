#!/bin/bash

# Enroot + Pyxis

export PROJECT_ROOT_AT=$SCRATCH/swiss-alignment/dev

srun \
  -J template-test \
  --pty \
  --container-image=$CONTAINER_IMAGES/claire+smoalla+swiss-alignment+amd64-cuda-root-latest.sqsh \
  --no-container-mount-home \
  --no-container-remap-root \
  --no-container-entrypoint \
  --container-writable \
  bash

exit 0
