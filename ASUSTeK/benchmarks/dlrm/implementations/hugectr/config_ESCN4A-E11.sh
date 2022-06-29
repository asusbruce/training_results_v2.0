## DL params
export BATCH_SIZE=55296
#export BATCH_SIZE=26148
export DGXNGPU=4

export CONFIG="escn4a-e11.py"

## System run parms
export DGXNNODES=1
export DGXSYSTEM=$(basename $(readlink -f ${BASH_SOURCE[0]}) | sed 's/^config_//' | sed 's/\.sh$//' )
export WALLTIME=00:10:00
export OMPI_MCA_btl="^openib"
export MOUNTS=/raid:/raid
export CUDA_DEVICE_MAX_CONNECTIONS=2
