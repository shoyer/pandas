"""
This file generates `intervaltree.pyx` which is then included in `../lib.pyx`
during building.  To regenerate `intervaltree.pyx`, just run:

    `python generate_intervaltree.py`.
"""
from __future__ import print_function
import os
from pandas.compat import StringIO
import numpy as np


warning_to_new_contributors = """
# DO NOT EDIT THIS FILE: This file was autogenerated from
# generate_intervaltree.py, so please edit that file and then run
# `python2 generate_intervaltree.py` to re-generate this file.
"""

header = r'''
from numpy cimport int64_t, float64_t
from numpy cimport ndarray, PyArray_ArgSort, NPY_QUICKSORT, PyArray_Take
import numpy as np

cimport cython
cimport numpy as cnp
cnp.import_array()

from hashtable cimport Int64Vector, Int64VectorData


ctypedef fused scalar64_t:
    float64_t
    int64_t


NODE_CLASSES = {}


cdef class IntervalTree(IntervalMixin):
    """A centered interval tree

    Based off the algorithm described on Wikipedia:
    http://en.wikipedia.org/wiki/Interval_tree
    """
    cdef:
        readonly object left, right, root
        readonly str closed
        object _left_sorter, _right_sorter

    def __init__(self, left, right, closed='right', leaf_size=100):
        """
        Parameters
        ----------
        left, right : np.ndarray[ndim=1]
            Left and right bounds for each interval. Assumed to contain no
            NaNs.
        closed : {'left', 'right', 'both', 'neither'}, optional
            Whether the intervals are closed on the left-side, right-side, both
            or neither. Defaults to 'right'.
        leaf_size : int, optional
            Parameter that controls when the tree switches from creating nodes
            to brute-force search. Tune this parameter to optimize query
            performance.
        """
        if closed not in ['left', 'right', 'both', 'neither']:
            raise ValueError("invalid option for 'closed': %s" % closed)

        left = np.asarray(left)
        right = np.asarray(right)
        dtype = np.result_type(left, right)
        self.left = np.asarray(left, dtype=dtype)
        self.right = np.asarray(right, dtype=dtype)

        indices = np.arange(len(left), dtype='int64')

        self.closed = closed

        node_cls = NODE_CLASSES[str(dtype), closed]
        self.root = node_cls(self.left, self.right, indices, leaf_size)

    @property
    def left_sorter(self):
        """How to sort the left labels; this is used for binary search
        """
        if self._left_sorter is None:
            self._left_sorter = np.argsort(self.left)
        return self._left_sorter

    @property
    def right_sorter(self):
        """How to sort the right labels
        """
        if self._right_sorter is None:
            self._right_sorter = np.argsort(self.right)
        return self._right_sorter

    def get_loc(self, scalar64_t key):
        """Return all positions corresponding to intervals that overlap with
        the given scalar key
        """
        result = Int64Vector()
        self.root.query(result, key)
        if not result.data.n:
            raise KeyError(key)
        return result.to_array()

    def _get_partial_overlap(self, key_left, key_right, side):
        """Return all positions corresponding to intervals with the given side
        falling between the left and right bounds of an interval query
        """
        if side == 'left':
            values = self.left
            sorter = self.left_sorter
        else:
            values = self.right
            sorter = self.right_sorter
        key = [key_left, key_right]
        i, j = values.searchsorted(key, sorter=sorter)
        return sorter[i:j]

    def get_loc_interval(self, key_left, key_right):
        """Lookup the intervals enclosed in the given interval bounds

        The given interval is presumed to have closed bounds.
        """
        import pandas as pd
        left_overlap = self._get_partial_overlap(key_left, key_right, 'left')
        right_overlap = self._get_partial_overlap(key_left, key_right, 'right')
        enclosing = self.get_loc(0.5 * (key_left + key_right))
        combined = np.concatenate([left_overlap, right_overlap, enclosing])
        uniques = pd.unique(combined)
        return uniques

    def get_indexer(self, scalar64_t[:] target):
        """Return the positions corresponding to unique intervals that overlap
        with the given array of scalar targets.
        """
        # TODO: write get_indexer_intervals
        cdef:
            int64_t old_len, i
            Int64Vector result

        result = Int64Vector()
        old_len = 0
        for i in range(len(target)):
            self.root.query(result, target[i])
            if result.data.n == old_len:
                result.append(-1)
            elif result.data.n > old_len + 1:
                raise KeyError(
                    'indexer does not intersect a unique set of intervals')
            old_len = result.data.n
        return result.to_array()

    def get_indexer_non_unique(self, scalar64_t[:] target):
        """Return the positions corresponding to intervals that overlap with
        the given array of scalar targets. Non-unique positions are repeated.
        """
        cdef:
            int64_t old_len, i
            Int64Vector result, missing

        result = Int64Vector()
        missing = Int64Vector()
        old_len = 0
        for i in range(len(target)):
            self.root.query(result, target[i])
            if result.data.n == old_len:
                result.append(-1)
                missing.append(i)
            old_len = result.data.n
        return result.to_array(), missing.to_array()

    def __repr__(self):
        return ('<IntervalTree: %s elements>'
                % self.root.n_elements)


cdef take(ndarray source, ndarray indices):
    """Take the given positions from a 1D ndarray
    """
    return PyArray_Take(source, indices, 0)


cdef sort_values_and_indices(all_values, all_indices, subset):
    indices = take(all_indices, subset)
    values = take(all_values, subset)
    sorter = PyArray_ArgSort(values, 0, NPY_QUICKSORT)
    sorted_values = take(values, sorter)
    sorted_indices = take(indices, sorter)
    return sorted_values, sorted_indices
'''

# we need specialized nodes and leaves to optimize for different dtype and
# closed values
# unfortunately, fused dtypes can't parameterize attributes on extension types,
# so we're stuck using template generation.

node_template = r'''
cdef class {dtype_title}Closed{closed_title}IntervalNode:
    """Non-terminal node for an IntervalTree

    Categorizes intervals by those that fall to the left, those that fall to
    the right, and those that overlap with the pivot.
    """
    cdef:
        {dtype_title}Closed{closed_title}IntervalNode left_node, right_node
        {dtype}_t[:] center_left_values, center_right_values, left, right
        int64_t[:] center_left_indices, center_right_indices, indices
        readonly {dtype}_t pivot
        readonly int64_t n_elements, n_center, leaf_size
        readonly bint is_leaf_node

    def __init__(self,
                 ndarray[{dtype}_t, ndim=1] left,
                 ndarray[{dtype}_t, ndim=1] right,
                 ndarray[int64_t, ndim=1] indices,
                 int64_t leaf_size):

        self.n_elements = len(left)
        self.leaf_size = leaf_size

        if self.n_elements <= leaf_size:
            # make this a terminal (leaf) node
            self.is_leaf_node = True
            self.left = left
            self.right = right
            self.indices = indices
            self.n_center
        else:
            # calculate a pivot so we can create child nodes
            self.is_leaf_node = False
            self.pivot = np.median(left + right) / 2
            left_set, right_set, center_set = self.classify_intervals(left, right)

            self.left_node = self.new_child_node(left, right, indices, left_set)
            self.right_node = self.new_child_node(left, right, indices, right_set)

            self.center_left_values, self.center_left_indices = \
                sort_values_and_indices(left, indices, center_set)
            self.center_right_values, self.center_right_indices = \
                sort_values_and_indices(right, indices, center_set)
            self.n_center = len(self.center_left_indices)

    @cython.wraparound(False)
    @cython.boundscheck(False)
    cdef classify_intervals(self, {dtype}_t[:] left, {dtype}_t[:] right):
        """Classify the given intervals based upon whether they fall to the
        left, right, or overlap with this node's pivot.
        """
        cdef:
            Int64Vector left_ind, right_ind, overlapping_ind
            Py_ssize_t i

        left_ind = Int64Vector()
        right_ind = Int64Vector()
        overlapping_ind = Int64Vector()

        for i in range(self.n_elements):
            if right[i] {cmp_right_converse} self.pivot:
                left_ind.append(i)
            elif self.pivot {cmp_left_converse} left[i]:
                right_ind.append(i)
            else:
                overlapping_ind.append(i)

        return (left_ind.to_array(),
                right_ind.to_array(),
                overlapping_ind.to_array())

    cdef new_child_node(self,
                        ndarray[{dtype}_t, ndim=1] left,
                        ndarray[{dtype}_t, ndim=1] right,
                        ndarray[int64_t, ndim=1] indices,
                        ndarray[int64_t, ndim=1] subset):
        """Create a new child node.
        """
        left = take(left, subset)
        right = take(right, subset)
        indices = take(indices, subset)
        return {dtype_title}Closed{closed_title}IntervalNode(
            left, right, indices, self.leaf_size)

    @cython.wraparound(False)
    @cython.boundscheck(False)
    @cython.initializedcheck(False)
    cdef query(self, Int64Vector result, scalar64_t point):
        """Recursively query this node and its sub-nodes for intervals that
        overlap with the query point.
        """
        cdef:
            int64_t[:] indices
            {dtype}_t[:] values
            Py_ssize_t i

        if self.is_leaf_node:
            # Once we get down to a certain size, it doesn't make sense to
            # continue the binary tree structure. Instead, we use linear
            # search.
            for i in range(self.n_elements):
                 if self.left[i] {cmp_left} point {cmp_right} self.right[i]:
                    result.append(self.indices[i])
        else:
            # There are child nodes. Based on comparing our query to the pivot,
            # look at the center values, then go to the relevant child.
            if point < self.pivot:
                values = self.center_left_values
                indices = self.center_left_indices
                for i in range(self.n_center):
                    if not values[i] {cmp_left} point:
                        break
                    result.append(indices[i])
                self.left_node.query(result, point)
            elif point > self.pivot:
                values = self.center_right_values
                indices = self.center_right_indices
                for i in range(self.n_center - 1, -1, -1):
                    if not point {cmp_right} values[i]:
                        break
                    result.append(indices[i])
                self.right_node.query(result, point)
            else:
                result.extend(self.center_left_indices)

    def __repr__(self):
        if self.is_leaf_node:
            return ('<{dtype_title}Closed{closed_title}IntervalNode: '
                    '%s elements (terminal)>' % self.n_elements)
        else:
            n_left = self.left_node.n_elements
            n_right = self.right_node.n_elements
            n_center = self.n_elements - n_left - n_right
            return ('<{dtype_title}Closed{closed_title}IntervalNode: pivot %s, '
                    '%s elements (%s left, %s right, %s overlapping)>' %
                    (self.pivot, self.n_elements, n_left, n_right, n_center))

    def counts(self):
        if self.is_leaf_node:
            return self.n_elements
        else:
            m = len(self.center_left_values)
            l = self.left_node.counts()
            r = self.right_node.counts()
            return (m, (l, r))

NODE_CLASSES['{dtype}', '{closed}'] = {dtype_title}Closed{closed_title}IntervalNode
'''


def generate_node_template():
    output = StringIO()
    for dtype in ['float64', 'int64']:
        for closed, cmp_left, cmp_right in [
                ('left', '<=', '<'),
                ('right', '<', '<='),
                ('both', '<=', '<='),
                ('neither', '<', '<')]:
            cmp_left_converse = '<' if cmp_left == '<=' else '<='
            cmp_right_converse = '<' if cmp_right == '<=' else '<='
            classes = node_template.format(dtype=dtype,
                                           dtype_title=dtype.title(),
                                           closed=closed,
                                           closed_title=closed.title(),
                                           cmp_left=cmp_left,
                                           cmp_right=cmp_right,
                                           cmp_left_converse=cmp_left_converse,
                                           cmp_right_converse=cmp_right_converse)
            output.write(classes)
            output.write("\n")
    return output.getvalue()


def generate_cython_file():
    # Put `intervaltree.pyx` in the same directory as this file
    directory = os.path.dirname(os.path.realpath(__file__))
    filename = 'intervaltree.pyx'
    path = os.path.join(directory, filename)

    with open(path, 'w') as f:
        print(warning_to_new_contributors, file=f)
        print(header, file=f)
        print(generate_node_template(), file=f)


if __name__ == '__main__':
    generate_cython_file()
