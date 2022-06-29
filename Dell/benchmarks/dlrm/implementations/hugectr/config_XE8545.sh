## DL params
export BATCH_SIZE=55296
export DGXNGPU=4

export CONFIG="xe8545_a100-40.py"

## System run parms
export DGXNNODES=1
export DGXSYSTEM=$(basename $(readlink -f ${BASH_SOURCE[0]}) | sed 's/^config_//' | sed 's/\.sh$//' )
#WALLTIME_BASE=$(( 5 + 30 * ${API_LOGGING:-0} ))
#WALLTIME_MINUTES=5
export WALLTIME=UNLIMITED
export OMPI_MCA_btl="^openib"
#export MOUNTS=/raid:/raid
export CUDA_DEVICE_MAX_CONNECTIONS=2
export NCCL_SOCKET_IFNAME=^eth,ib
