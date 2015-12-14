#!/usr/bin/python3 -tt
#---------------------------------------------------------------------------
# Assistance routines for handling book allocation policy.
# Designed for "Full Rack Demo" (FRD) to be launched in the summer of 2016.
#---------------------------------------------------------------------------

import errno
import os
import sys
from pdb import set_trace

from book_shelf_bos import TMBook
from frdnode import FRDnode, FRDintlv_group

#--------------------------------------------------------------------------
# lfs_fuse.py does a little syntax/error checking before calling *_xattr
# routines.  xattr_assist does more and does the math for some of the
# user.LFS.xxxxx intrinsics.  Start with general logic checks.


class BookPolicy(object):

    POLICY_DEFAULT = 'LocalOnly'
    _policies = (POLICY_DEFAULT, 'LocalFirst', 'Random')

    @classmethod
    def xattr_assist(cls, LCEobj, cmdict, setting=False, removing=False):
        '''More error checking and legwork for user.LFS.xxxxx.  LCEobj is
           both data to be used and returns the error on a Raise.'''
        LCEobj.errno = errno.EINVAL
        xattr = cmdict['xattr']
        value = cmdict.get('value', None)
        if setting:
            assert value is not None, 'Trying to set a null value'
        elems = xattr.split('.')
        if elems[1] != 'LFS':       # simple get or set, leave it to caller
            return (xattr, value)

        # LFS special values
        assert len(elems) == 3, 'LFS xattrs are of form "user.LFS.xxx"'
        assert not removing, 'Removal of LFS xattrs is prohibited'

        if elems[2] == 'AllocationPolicy':
            if setting:
                assert value in cls._policies, \
                    'Bad AllocationPolicy "%s"' % value
        elif elems[2] == 'Interleave':
            assert not setting, 'Setting Interleave is prohibited'
            shelf = LCEobj.cmd_get_shelf(cmdict)
            bos = LCEobj.db.get_books_on_shelf(shelf)
            value = bytes([ b.intlv_group for b in bos ]).decode()
        else:
            raise AssertionError('Bad LFS attribute "%s"' % xattr)
        return (xattr, value)

   #-----------------------------------------------------------------------
    # Return a list of books or raise an error.

    def __init__(self, LCEobj, shelf, context):
        assert len(LCEobj.IGs) == len(LCEobj.nodes), 'IG:node != 1:1'
        LCEobj.errno = errno.EINVAL
        self.LCEobj = LCEobj
        self.shelf = shelf
        self.context = context
        self.name = LCEobj.db.get_xattr(shelf, 'user.LFS.AllocationPolicy')
        assert self.name in self._policies, 'Unknown policy "%s"' % self.name

    def __str__(self):
        return '%s policy=%s' % (self.shelf.name, self.name)

    def __repr__(self):
        return self.__str__()

    def _policy_LocalOnly(self, books_needed, inverse=False):
        # using IGs 0-79 on nodes 1-80
        IG = self.context['node_id'] - 1
        db = self.LCEobj.db
        freebooks = db.get_books_by_intlv_group(
            IG, TMBook.ALLOC_FREE, books_needed, inverse)
        return freebooks

    def _policy_LocalFirst(self, books_needed):
        localbooks = self._policy_LocalOnly(books_needed)
        books_needed -= len(localbooks)
        assert books_needed >= 0, 'LocalFirst policy internal error'
        if not books_needed:
            return localbooks
        nonlocalbooks = self._policy_LocalOnly(books_needed, inverse=True)
        return localbooks + nonlocalbooks

    def __call__(self, books_needed):
        self.LCEobj.errno = errno.EINVAL
        if self.name == self.POLICY_DEFAULT:
            return self._policy_LocalOnly(books_needed)
        elif self.name == 'LocalFirst':
            return self._policy_LocalFirst(books_needed)
        else:
            raise AssertionError('%s not implemented yet' % self.name)
