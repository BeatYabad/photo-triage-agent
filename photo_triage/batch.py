"""Bounded incremental batches. Caller retains inventory and action ownership."""
from itertools import islice


def batches(rows, limit=500):
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError('Batch size must be an integer from 1 to 10000')
    pending = iter(rows)
    while batch := list(islice(pending, limit)):
        yield batch


def process_batches(rows, process_many, limit=500):
    """Preserve order/checkpoints while bounding each scheduling window."""
    for batch in batches(rows, limit):
        yield from process_many(batch)
