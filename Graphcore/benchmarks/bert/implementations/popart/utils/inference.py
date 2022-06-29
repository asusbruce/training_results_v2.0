import os
import time
import logging
from collections import defaultdict
from contextlib import contextmanager
from typing import DefaultDict, Iterable
import numpy as np
import popart

logger = logging.getLogger(__name__)


@contextmanager
def realtime_scheduling(enable=True):
    '''Use a system call to enable real-time scheduling for the whole process:'''
    pid = os.getpid()
    if enable:
        logger.info(f"Enabling real-time scheduler for process: PID {pid}")
        os.system(f"sudo -n chrt --rr -p 99 {pid}")
    yield None
    if enable:
        logger.info(f"Disabling real-time scheduler for process: PID {pid}")
        os.system(f"sudo -n chrt --other -p 0 {pid}")


def create_callback_stepio(data: dict,
                           anchors: dict,
                           start_times: DefaultDict[str, list],
                           end_times: DefaultDict[str, list],
                           batches_per_step: int):
    '''Create a popart.PyStepIOCallback using data and anchors.
        Will record timing information in start_times and end_times.'''

    micro_batch_indices = defaultdict(int)

    # Input callback is called when the data is needed:
    def input_callback(tensor_id: str, is_prefetch: bool):
        input_time = time.perf_counter()
        start_times[tensor_id].append(input_time)
        return data[tensor_id][micro_batch_indices[tensor_id]]

    # Called after the input buffer has been consumed by the device:
    def input_complete_callback(tensor_id: str):
        micro_batch_indices[tensor_id] = \
            (micro_batch_indices[tensor_id] + 1) % batches_per_step
        return

    # Output callback is called when a buffer is needed for the result:
    def output_callback(tensor_id: str):
        return anchors[tensor_id][micro_batch_indices[tensor_id]]

    # Complete callback is called when the output buffer has
    # been filled (result is ready to be consumed by the host):
    def output_complete_callback(tensor_id: str):
        output_time = time.perf_counter()
        end_times[tensor_id].append(output_time)
        micro_batch_indices[tensor_id] = \
            (micro_batch_indices[tensor_id] + 1) % batches_per_step

    stepio = popart.PyStepIOCallback(input_callback,
                                     input_complete_callback,
                                     output_callback,
                                     output_complete_callback)
    return stepio


def get_timing_start_anchor(start_times: DefaultDict[str, list]):
    '''Return the ID of the first input that is sent from the host.
    Order is repeateable so we can just check the time for one entry'''
    return min(start_times, key=lambda k: start_times[k][-1])


def get_timing_end_anchor(end_times: DefaultDict[str, list]):
    '''Return the ID of the last anchor that is returned to the host.
    Order is repeateable so we can just check the time for one entry.'''
    return max(end_times, key=lambda k: end_times[k][-1])


def compute_latency_from_durations(durations: Iterable[float]):
    mean_latency = np.average(durations)
    min_latency = min(durations)
    max_latency = max(durations)
    return mean_latency, min_latency, max_latency


def compute_latency_from_callbacks(start_times: DefaultDict[str, list],
                                   end_times: DefaultDict[str, list],
                                   batches_per_step: int):
    '''Compute latency stats using time between the
    two tensors most separated in time'''
    start_id = get_timing_start_anchor(start_times)
    end_id = get_timing_end_anchor(end_times)
    rtts = list(
        map(lambda v: v[1] - v[0], zip(start_times[start_id], end_times[end_id])))
    if len(rtts) != batches_per_step:
        raise RuntimeError(
            "Number of timings doesn't match items in the batch. Something is wrong.")
    mean_latency = (sum(rtts)) / batches_per_step
    min_latency = min(rtts)
    max_latency = max(rtts)
    if (logging.getLogger().isEnabledFor(logging.DEBUG)):
        for i, v in enumerate(rtts):
            logging.debug(f"LATENCY: {i} {v}")
    return mean_latency, min_latency, max_latency
