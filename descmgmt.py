#!/usr/bin/python -tt

import time

import array
import fcntl
import os
import stat
import struct
from pdb import set_trace

# Python3: __cmp__ went away, use this decorator with __lt__ and __eq__
# to achieve the same thing.
from functools import total_ordering

from genericobj import GenericObject
import tm_ioctl_opt as IOCTL

@total_ordering
class _LZAinuse(GenericObject):
    '''Maintain stats for an LZA which represents an aperture base:
       - aperture index
       - PIDs that have faulted at least once
       - number of faulting pages behind that PID'''

    def __init__(self, baseLZA, index, pid=0, userVA=0, isBook=True):
        assert isBook, 'Booklets are not yet implemented'
        self.baseLZA = baseLZA
        self.index = index
        self.pids = { }
        self.mtime = 0                          # Force __lt__, unless...
        if pid and userVA:
            self.pids[pid] = [userVA, ]
            self.mtime = int(time.time())       # epoch
        self.isBook = isBook

    def update(self, pid, userVA):
        '''"self" was selected by LZA, so anything is possible'''
        try:
            self.pids[pid].append(userVA)   # Existing process, new fault
        except KeyError:
            self.pids[pid] = [userVA, ]     # New process, first fault
        self.mtime = int(time.time())       # epoch

    # Comparisons for hash().  First age, then by total mappings (pages).
    def __eq__(self, other):
        try:
            if self.mtime != other.mtime:
                return False
            selfmappings = sum(v for v in self.pids.values())
            othermappings = sum(v for v in other.pids.values())
            return selfmappings == othermappings
        except Exception as e:
            return NotImplemented

    # Comparisons for min().  First age, then by total mappings (pages).
    def __lt__(self, other):
        set_trace()
        try:
            if self.mtime >= other.mtime:
                return False
            selfmappings = sum(len(v) for v in self.pids.values())
            othermappings = sum(len(v) for v in other.pids.values())
            return selfmappings < othermappings
        except Exception as e:
            return NotImplemented

class DescMgmt(GenericObject):

    _descioctl = '/dev/descioctl'
    _DESBK_READ_OFF = 0xc0102100    # IOWR('!', ...)
    _DESBK_PUT      = 0xc0102102    # IOW('!', ...)

    _IG_SHIFT = 46                  # Bits of offset for 7 bit IG
    _IG_MASK = ((1 << 7) - 1)       # Mask for 7 bit IG
    _BOOK_SHIFT = 33                # Bits of offset for 20 bit book number
    _BOOK_MASK = ((1 << 13) - 1)    # Mask for 13 bit book number
    _BOOKLET_SHIFT = 16             # Bits of offset for 17 bit booklet number
    _BOOKLET_MASK = ((1 << 17) - 1) # Mask for 17 bit booklet number

    _evenmask = (2**64) - 1 - 1     # one for 64 bits of 1s, then clear the LSB

    def __init__(self, args, indices=None):
        self.verbose = args.verbose
        self._enabled = args.shadow_apertures
        if not self._enabled:
            if self.verbose:
                print('Descriptor management disabled')
            return
        try:
            tmp = os.stat(self._descioctl)
            assert tmp.st_mode & stat.S_IFCHR == stat.S_IFCHR   # man 2 stat
        except Exception as e:
            raise AssertionError('Missing %s' % self._descioctl)
        if indices is None:
            self._indices = (0, 1, 2)
        else:
            assert min(indices) >=0 and max(indices) < 2000, 'Bad index range'
            self._indices = tuple(indices)
        self._available = [ ]
        self._descriptors = { }    # track pages inside a book
        out = [ ]
        for index, desc in enumerate(self.descTable):
            out.append(hex(desc))
            if desc & 1:   # Descriptor valid bit
                LZA = (desc & self._evenmask) >> self._BOOK_SHIFT
                self._descriptors[LZA] = _LZAinuse(LZA, index)
            else:
                self._available.append(index)
        if self.verbose > 2:
            print(','.join(out))

    def _consistent(self):
        assert len(self._available) + len(self._descriptors) == len(self._indices), 'MEBST INCONSISTENT DESCRIPTORS'

    def buffer2longs(self, index=None, value=0):
        '''Make an array of 2 longs.'''
        if index is None:
            index = 999999999
        else:
            assert index in self._indices, 'Bad aperture index'
        return array.array('Q', [index, value])

    @property
    def descTable(self):
        if not self._enabled:
            return None
        buf = self.buffer2longs()
        desbk = [ ]
        with open(self._descioctl, 'wb') as f:
            for index in self._indices:
                buf[0] = index
                try:
                    junk = fcntl.ioctl(f, self._DESBK_READ_OFF, buf)
                    desbk.append(buf[1])
                except Exception as e:
                    break
        assert len(desbk) == len(self._indices), 'Bad descriptor table read'
        return desbk

    def desbk_set(self, index, baseLZA):
        '''Convert baseLZA (20 bits of IG:booknum) to valid descriptor entry'''
        if not self._enabled:
            return
        assert 0 <= baseLZA < 2**20, 'baseLZA out of range'
        # LSB is the valid bit
        buf = self.buffer2longs(index, (baseLZA << self._BOOK_SHIFT) + 1)
        with open(self._descioctl, 'wb') as f:
            junk = fcntl.ioctl(f, self._DESBK_PUT, buf)

    def assign(self, baseLZA, pid, userVA, isBook=True):
        '''Find a descriptor for the faulting LZA and PID. baseLZA is the
           20 bit IG:booknum value (per book_register.py). Return value may
           be None if an unused descriptor was available, or the LZA and PIDs
           to evict to make room.'''

        if not self._enabled:
            return

        assert 0 <= baseLZA < 2**20, 'baseLZA out of range'
        self._consistent()
        existing = self._descriptors.get(baseLZA, None)
        if existing is not None:    # at least one pid exists in dict
            existing.update(pid, userVA)
            return None

        # If an aperture is available, take it, else evict/reuse
        if self._available:
            try:
                index = self._available[0]
                newLZA = _LZAinuse(baseLZA, index, pid, userVA, isBook)
                self.desbk_set(index, baseLZA)
                self._descriptors[baseLZA] = newLZA
                self._available.pop(0)
                self._consistent()  # trap this one
            except AssertionError as e:
                set_trace()
                pass
            except Exception as e:
                set_trace()
                pass
            self._consistent()      # pass this one up
            return None

        evictLZA = min(self._descriptors.values())
        newLZA = _LZAinuse(baseLZA, evictLZA.index, pid, userVA, isBook)

        # Chickens and eggs are here as I shouldn't reprogram the descriptor
        # table until flushing/PTE invalidation has occurred for all affected
        # PIDs.
        return GenericObject(evictLZA=evictLZA, newLZA=newLZA)

