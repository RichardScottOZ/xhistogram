"""
Numpy API for xhistogram.
"""


import numpy as np
from functools import reduce
from numpy import (
    digitize,
    bincount,
    reshape,
    ravel_multi_index,
    concatenate,
    broadcast_arrays,
)
from .duck_array_ops import _any_dask_array


def _ensure_bins_is_a_list_of_arrays(bins, N_expected):
    if len(bins) == N_expected:
        return bins
    elif N_expected == 1:
        return [bins]
    else:
        raise ValueError("Can't figure out what to do with bins.")


def _bincount_2d(bin_indices, weights, N, hist_shapes):
    # a trick to apply bincount on an axis-by-axis basis
    # https://stackoverflow.com/questions/40591754/vectorizing-numpy-bincount
    # https://stackoverflow.com/questions/40588403/vectorized-searchsorted-numpy
    M = bin_indices.shape[0]
    if weights is not None:
        weights = weights.ravel()
    bin_indices_offset = (bin_indices + (N * np.arange(M)[:, None])).ravel()
    bc_offset = bincount(bin_indices_offset, weights=weights, minlength=N * M)
    final_shape = (M,) + tuple(hist_shapes)
    return bc_offset.reshape(final_shape)


def _bincount_loop(bin_indices, weights, N, hist_shapes, block_chunks):
    M = bin_indices.shape[0]
    assert sum(block_chunks) == M
    block_counts = []
    # iterate over chunks
    bounds = np.cumsum((0,) + block_chunks)
    for m_start, m_end in zip(bounds[:-1], bounds[1:]):
        bin_indices_block = bin_indices[m_start:m_end]
        weights_block = weights[m_start:m_end] if weights is not None else None
        bc_block = _bincount_2d(bin_indices_block, weights_block, N, hist_shapes)
        block_counts.append(bc_block)
    all_counts = concatenate(block_counts)
    final_shape = (bin_indices.shape[0],) + tuple(hist_shapes)
    return all_counts.reshape(final_shape)


def _determine_block_chunks(bin_indices, block_size):
    M, N = bin_indices.shape
    if block_size is None:
        return (M,)
    if block_size == "auto":
        try:
            # dask arrays - use the pre-existing chunks
            chunks = bin_indices.chunks
            return chunks[0]
        except AttributeError:
            # automatically pick a chunk size
            # this a a heueristic without much basis
            _MAX_CHUNK_SIZE = 10_000_000
            block_size = min(_MAX_CHUNK_SIZE // N, M)
    assert isinstance(block_size, int)
    num_chunks = M // block_size
    block_chunks = num_chunks * (block_size,)
    residual = M % block_size
    if residual:
        block_chunks += (residual,)
    assert sum(block_chunks) == M
    return block_chunks


def _dispatch_bincount(bin_indices, weights, N, hist_shapes, block_size=None):
    # block_chunks is like a dask chunk, a tuple that divides up the first
    # axis of bin_indices
    block_chunks = _determine_block_chunks(bin_indices, block_size)
    if len(block_chunks) == 1:
        # single global chunk, don't need a loop over chunks
        return _bincount_2d(bin_indices, weights, N, hist_shapes)
    else:
        return _bincount_loop(bin_indices, weights, N, hist_shapes, block_chunks)


def _bincount_2d_vectorized(
    *args, bins=None, weights=None, density=False, right=False, block_size=None
):
    """Calculate the histogram independently on each row of a 2D array"""

    N_inputs = len(args)
    a0 = args[0]

    # consistency checks for inputa
    for a, b in zip(args, bins):
        assert a.ndim == 2
        assert b.ndim == 1
        assert a.shape == a0.shape
    if weights is not None:
        assert weights.shape == a0.shape

    nrows, ncols = a0.shape
    nbins = [len(b) for b in bins]
    hist_shapes = [nb + 1 for nb in nbins]

    # a marginally faster implementation would be to use searchsorted,
    # like numpy histogram itself does
    # https://github.com/numpy/numpy/blob/9c98662ee2f7daca3f9fae9d5144a9a8d3cabe8c/numpy/lib/histograms.py#L864-L882
    # for now we stick with `digitize` because it's easy to understand how it works

    # Add small increment to the last bin edge to make the final bin right-edge inclusive
    # Note, this is the approach taken by sklearn, e.g.
    # https://github.com/scikit-learn/scikit-learn/blob/master/sklearn/calibration.py#L592
    # but a better approach would be to use something like _search_sorted_inclusive() in
    # numpy histogram. This is an additional motivation for moving to searchsorted
    bins = [np.concatenate((b[:-1], b[-1:] + 1e-8)) for b in bins]

    # the maximum possible value of of digitize is nbins
    # for right=False:
    #   - 0 corresponds to a < b[0]
    #   - i corresponds to bins[i-1] <= a < b[i]
    #   - nbins corresponds to a a >= b[1]
    each_bin_indices = [digitize(a, b) for a, b in zip(args, bins)]
    # product of the bins gives the joint distribution
    if N_inputs > 1:
        bin_indices = ravel_multi_index(each_bin_indices, hist_shapes)
    else:
        bin_indices = each_bin_indices[0]
    # total number of unique bin indices
    N = reduce(lambda x, y: x * y, hist_shapes)

    bin_counts = _dispatch_bincount(
        bin_indices, weights, N, hist_shapes, block_size=block_size
    )

    # just throw out everything outside of the bins, as np.histogram does
    # TODO: make this optional?
    slices = (slice(None),) + (N_inputs * (slice(1, -1),))
    bin_counts = bin_counts[slices]

    return bin_counts


def _bincount(*all_arrays, weights=False, axis=None, bins=None, density=None):

    # is this necessary?
    all_arrays_broadcast = broadcast_arrays(*all_arrays)

    a0 = all_arrays_broadcast[0]

    do_full_array = (axis is None) or (set(axis) == set(range(a0.ndim)))

    if do_full_array:
        kept_axes_shape = (1,) * a0.ndim
    else:
        kept_axes_shape = tuple(
            [a0.shape[i] if i not in axis else 1 for i in range(a0.ndim)]
        )

    def reshape_input(a):
        if do_full_array:
            d = a.ravel()[None, :]
        else:
            # reshape the array to 2D
            # axis 0: preserved axis after histogram
            # axis 1: calculate histogram along this axis
            new_pos = tuple(range(-len(axis), 0))
            c = np.moveaxis(a, axis, new_pos)
            split_idx = c.ndim - len(axis)
            dims_0 = c.shape[:split_idx]
            # assert dims_0 == kept_axes_shape
            dims_1 = c.shape[split_idx:]
            new_dim_0 = np.prod(dims_0)
            new_dim_1 = np.prod(dims_1)
            d = reshape(c, (new_dim_0, new_dim_1))
        return d

    all_arrays_reshaped = [reshape_input(a) for a in all_arrays_broadcast]

    if weights:
        weights_array = all_arrays_reshaped.pop()
    else:
        weights_array = None

    bin_counts = _bincount_2d_vectorized(
        *all_arrays_reshaped, bins=bins, weights=weights_array, density=density
    )

    final_shape = kept_axes_shape + bin_counts.shape[1:]
    bin_counts = reshape(bin_counts, final_shape)

    return bin_counts


def histogram(
    *args, bins=None, axis=None, weights=None, density=False, block_size="auto"
):
    """Histogram applied along specified axis / axes.

    Parameters
    ----------
    args : array_like
        Input data. The number of input arguments determines the dimensonality
        of the histogram. For example, two arguments prodocue a 2D histogram.
        All args must have the same size.
    bins :  int or array_like or a list of ints or arrays, optional
        If a list, there should be one entry for each item in ``args``.
        The bin specification:

          * If int, the number of bins for all arguments in ``args``.
          * If array_like, the bin edges for all arguments in ``args``.
          * If a list of ints, the number of bins  for every argument in ``args``.
          * If a list arrays, the bin edges for each argument in ``args``
            (required format for Dask inputs).
          * A combination [int, array] or [array, int], where int
            is the number of bins and array is the bin edges.

        When bin edges are specified, all but the last (righthand-most) bin include
        the left edge and exclude the right edge. The last bin includes both edges.

        A ``TypeError`` will be raised if ``args`` contains dask arrays and
        ``bins`` are not specified explicitly as a list of arrays.
    axis : None or int or tuple of ints, optional
        Axis or axes along which the histogram is computed. The default is to
        compute the histogram of the flattened array
    weights : array_like, optional
        An array of weights, of the same shape as `a`.  Each value in
        `a` only contributes its associated weight towards the bin count
        (instead of 1). If `density` is True, the weights are
        normalized, so that the integral of the density over the range
        remains 1.
    density : bool, optional
        If ``False``, the result will contain the number of samples in
        each bin. If ``True``, the result is the value of the
        probability *density* function at the bin, normalized such that
        the *integral* over the range is 1. Note that the sum of the
        histogram values will not be equal to 1 unless bins of unity
        width are chosen; it is not a probability *mass* function.
    block_size : int or 'auto', optional
        A parameter which governs the algorithm used to compute the histogram.
        Using a nonzero value splits the histogram calculation over the
        non-histogram axes into blocks of size ``block_size``, iterating over
        them with a loop (numpy inputs) or in parallel (dask inputs). If
        ``'auto'``, blocks will be determined either by the underlying dask
        chunks (dask inputs) or an experimental built-in heuristic (numpy inputs).

    Returns
    -------
    hist : array
        The values of the histogram.

    See Also
    --------
    numpy.histogram, numpy.bincount, numpy.digitize
    """

    a0 = args[0]
    ndim = a0.ndim

    if axis is not None:
        axis = np.atleast_1d(axis)
        assert axis.ndim == 1
        axis_normed = []
        for ax in axis:
            if ax >= 0:
                ax_positive = ax
            else:
                ax_positive = ndim + ax
            assert ax_positive < ndim, "axis must be less than ndim"
            axis_normed.append(ax_positive)
        axis = [int(i) for i in axis_normed]

    all_arrays = list(args)
    n_inputs = len(all_arrays)

    if weights is not None:
        all_arrays.append(weights)
        has_weights = True
    else:
        has_weights = False

    dtype = "i8" if not has_weights else weights.dtype

    bins = _ensure_bins_is_a_list_of_arrays(bins, n_inputs)

    bincount_kwargs = dict(weights=has_weights, axis=axis, bins=bins, density=density)

    # here I am assuming all the arrays have the same shape
    # probably needs to be generalized
    input_indexes = [tuple(range(a.ndim)) for a in all_arrays]
    input_index = input_indexes[0]
    assert all([ii == input_index for ii in input_indexes])

    # keep these axes in the inputs
    if axis is not None:
        drop_axes = tuple([ii for ii in input_index if ii in axis])
    else:
        drop_axes = input_index

    if _any_dask_array(weights, *all_arrays):
        # We should be able to just apply the bin_count function to every
        # block and then sum over all blocks to get the total bin count.
        # The main challenge is to figure out the chunk shape that will come
        # out of _bincount. We might also need to add dummy dimensions to sum
        # over in the _bincount function
        import dask.array as dsa

        # Important note from blockwise docs
        # > Any index, like i missing from the output index is interpreted as a contraction...
        # > In the case of a contraction the passed function should expect an iterable of blocks
        # > on any array that holds that index.
        # This means that we need to have all the input indexes present in the output index
        # However, they will be reduced to singleton (len 1) dimensions

        adjust_chunks = {i: (lambda x: 1) for i in drop_axes}

        new_axes = {
            max(input_index) + 1 + i: axis_len
            for i, axis_len in enumerate([len(bin) - 1 for bin in bins])
        }
        out_index = input_index + tuple(new_axes)

        blockwise_args = []
        for arg in all_arrays:
            blockwise_args.append(arg)
            blockwise_args.append(input_index)

        bin_counts = dsa.blockwise(
            _bincount,
            out_index,
            *blockwise_args,
            new_axes=new_axes,
            adjust_chunks=adjust_chunks,
            meta=np.array((), dtype),
            align_arrays=False,
            **bincount_kwargs,
        )
        # sum over the block dims
        bin_counts = bin_counts.sum(drop_axes)
    else:
        # drop the extra axis used for summing over blocks
        bin_counts = _bincount(*all_arrays, **bincount_kwargs).squeeze(drop_axes)

    if density:
        # Normalise by dividing by bin counts and areas such that all the
        # histogram data integrated over all dimensions = 1
        bin_widths = [np.diff(b) for b in bins]
        if n_inputs == 1:
            bin_areas = bin_widths[0]
        elif n_inputs == 2:
            bin_areas = np.outer(*bin_widths)
        else:
            # Slower, but N-dimensional logic
            bin_areas = np.prod(np.ix_(*bin_widths))

        h = bin_counts / bin_areas / bin_counts.sum()
    else:
        h = bin_counts

    return h
