#!/usr/bin/python3 -tt

# Support classes for two different types of shadow files, before direct
# kernel support of mmap().  Shadowing must accommodate graceless restarts
# including book_register.py rewrites.

import errno
import math
import os
import stat
import sys
import tempfile
import mmap
from copy import deepcopy
from subprocess import getoutput

from pdb import set_trace

from tm_fuse import TmfsOSError, tmfs_get_context

from descmgmt import DescriptorManagement

#--------------------------------------------------------------------------
# _shelfcache is essentially a copy of the Librarian's "opened_shelves"
# table data generated on the fly.  The goal was to avoid a round trip
# to the Librarian for many FuSE interactions.  This class duck-types a
# dict with multiple keys (name and file handles).  The value is a single,
# modified TMShelf object that holds all open-related data.  The data
# will assist PTE management for processes holding the shelf open.
# Account for multiple opens by same PID as well as different PIDs.
# Multinode support will require OOB support and a few more Librarian calls.

class shadow_support(object):
    '''Provide private data storage for subclasses.'''

    _mode_rw_file = stat.S_IFREG + 0o600  # regular file

    def __init__(self, args, lfs_globals):
        self.verbose = args.verbose
        self.book_size = lfs_globals['book_size_bytes']
        self._shelfcache = { }

        # Replaces ig_gap calculation.

        offset = 0
        self._igstart = {}
        for igstr in sorted(lfs_globals['books_per_IG'].keys()):
            ig = int(igstr)
            if self.verbose > 2:
                print('IG %2d flatspace offset @ %d (0x%x)' % (ig, offset, offset))
            self._igstart[ig] = offset
            books = int(lfs_globals['books_per_IG'][igstr])
            offset += books * self.book_size
        self.book_shift = int(math.log(self.book_size, 2))

    def __setitem__(self, fh, shelf):
        '''Part of the support for duck-typing a dict with multiple keys.'''
        assert isinstance(fh, int), 'Only integer fh is expected as key'
        pid = tmfs_get_context()[2]
        cached = self._shelfcache.get(shelf.name, None)
        if cached is None:
            # Create a copy because "open_handle" will be redefined.  This
            # single copy will be retrievable by the shelf name and all of
            # its open handles.  The copy itself has a list of the fh keys
            # indexed by pid, so open_handle.keys() is all the pids.
            cached = deepcopy(shelf)
            self._shelfcache[cached.name] = cached
            self._shelfcache[fh] = cached
            cached.open_handle = { }
            cached.open_handle[pid] = [ fh, ]
            return

        # Has the shelf changed somehow?  If so, replace the cached copy
        # and perhaps take other steps.  Break down the comparisons in
        # book_shelf_bos.py::TMShelf::__eq__()
        if cached != shelf:
            invalidate = True   # and work to make it false
            assert cached.id == shelf.id, 'Shelf aliasing error?'  # TSNH :-)

            # If it grew, are the first "n" books still the same?
            if shelf.size_bytes > cached.size_bytes:
                for i, book in enumerate(cached.bos):
                    if book != shelf.bos[i]:
                        break
                else:
                    invalidate = False  # the first "n" books match
            # look at cached to get references for replacement
            for vlist in cached.open_handle.values():
                for fh in vlist:
                    self._shelfcache[fh] = shelf
            self._shelfcache[shelf.name] = shelf
            shelf.open_handle = cached.open_handle
            if invalidate:
                print('\n\tNEED TO INVALIDATE PTES!!!\n')
            return

        # fh is unique (created by Librarian as table index).  Paranoia check,
        # then add the new key.
        try:
            all_fh = [ ]
            for vlist in cached.open_handle.values():
                all_fh += vlist
            assert fh not in all_fh, 'Duplicate fh in open_handle'
            self._shelfcache[fh] = cached
            try:
                cached.open_handle[pid].append(fh)
            except KeyError as e:
                cached.open_handle[pid] = [ fh, ]
        except Exception as e:
            print(str(e))
            set_trace()
            raise

    def __getitem__(self, key):
        '''Part of the support for duck-typing a dict with multiple keys.
           Suppress KeyError, returning None if no value exists.'''
        return self._shelfcache.get(key, None)

    def __contains__(self, key):
        '''Part of the support for duck-typing a dict with multiple keys.'''
        return key in self._shelfcache

    def __delitem__(self, key):
        '''Part of the support for duck-typing a dict with multiple keys.'''
        is_fh = isinstance(key, int)
        try:
            cached = self._shelfcache[key]
        except KeyError as e:
            # Not currently open, something like "rm somefile"
            if not is_fh:
                return
            raise AssertionError('Deleting a missing fh?')

        del self._shelfcache[key]   # always
        if is_fh:
            # Remove this direct shelf reference plus the back link
            open_handles = cached.open_handle
            for pid, fhlist in open_handles.items():
                if key in fhlist:
                    fhlist.remove(key)
                    if not fhlist:
                        del open_handles[pid]
                    if not open_handles:            # Last reference
                        del self._shelfcache[cached.name]
                return
            # There has to be one
            raise AssertionError('Cannot find fh to delete')

        # It's a string so remove the whole thing.  This is only called
        # from unlink; so VFS has done the filtering job on open handles.
        if cached.open_handle is None:  # probably "unlink"ing
            return
        all_fh = [ ]
        open_handles = cached.open_handle
        if open_handles is not None:
            for vlist in cached.open_handle.values():
                all_fh += vlist
            all_fh = frozenset(all_fh)  # paranoid: remove dupes
            for fh in all_fh:
                del self._shelfcache[fh]

    def keys(self):
        '''Part of the support for duck-typing a dict with multiple keys.'''
        return self._shelfcache.keys()

    def items(self):
        '''Part of the support for duck-typing a dict with multiple keys.'''
        return self._shelfcache.items()

    def values(self):
        '''Part of the support for duck-typing a dict with multiple keys.'''
        return self._shelfcache.values()

    # End of dictionary duck typing, now use that cache

    def shadow_offset(self, shelf_name, shelf_offset):
        '''Translate shelf-relative offset to flat shadow file offset'''
        bos = self[shelf_name].bos
        bos_index = shelf_offset // self.book_size  # (0..n)

        # Stop FS read ahead past shelf, but what about writes?  Later.
        try:
            book = bos[bos_index]
        except Exception as e:
            return -1

        # Offset into flat space has several contributors.  Oddly enough
        # this doesn't neet the concatenated LZA field.
        intlv_group = book['intlv_group']
        book_num = book['book_num']
        book_start = book_num * self.book_size
        book_offset = shelf_offset % self.book_size
        tmp = self._igstart[intlv_group] + book_start + book_offset
        return tmp

    # Provide ABC noop defaults.  Note they're not all actually noop.
    # Top men are insuring this works with multiple opens of a shelf.

    def truncate(self, shelf, length, fh):
        if fh is not None:
            assert fh in self._shelfcache, 'VFS thinks %s is open but LFS does not' % shelf.name
        return 0

    def unlink(self, shelf_name):
        try:
            del self[shelf_name]
        except Exception as e:
            set_trace()
            raise
        return 0

    # "man fuse" regarding "hard_remove": an "rm" of a file with active
    # opens tries to rename it.
    def rename(self, old, new):
        try:
            # Retrieve shared object, fix it, and rebind to new name.
            cached = self._shelfcache[old]
            cached.name = new
            del self._shelfcache[old]
            self._shelfcache[new] = cached
        except KeyError as e:
            if new.startswith('.tmfs_hidden'):
                # VFS thinks it's there so I should too
                raise TmfsOSError(errno.ESTALE)
        return 0

    def release(self, fh):  # shadow_support
        retval = deepcopy(self[fh])
        retval.open_handle = fh
        del self[fh]
        return retval

    # Piggybacked during mmap fault handling.  If the kernel receives
    # 'FALLBACK' it will use legacy, generic cache-based handler with stock
    # read() and write() spill and fill.  Override to do true mmaps.
    def getxattr(self, shelf_name, xattr):
        return 'FALLBACK'

    def read(self, shelf_name, length, offset, fd):
        raise TmfsOSError(errno.ENOSYS)

    def write(self, shelf_name, buf, offset, fd):
        raise TmfsOSError(errno.ENOSYS)

#--------------------------------------------------------------------------


class shadow_directory(shadow_support):
    '''Create a regular file for each shelf as backing store.  These files
       will exist in the file system of the entity running lfs_fuse,
       ie, if you're on a VM, the file storage is in the VM disk image.'''

    def __init__(self, args, lfs_globals):
        super(self.__class__, self).__init__(args, lfs_globals)
        assert os.path.isdir(
            args.shadow_dir), 'No such directory %s' % args.shadow_dir
        self._shadowpath = args.shadow_dir
        try:
            probe = tempfile.TemporaryFile(dir=args.shadow_dir)
            probe.close()
        except OSError as e:
            raise RuntimeError('%s is not writeable' % args.shadow_dir)

    def shadowpath(self, shelf_name):
        return '%s/%s' % (self._shadowpath, shelf_name)

    def unlink(self, shelf_name):
        # FIXME: not tested since unlink was expanded to do zeroing
        for k, v in self.items():
            if v[0].name == shelf_name:
                del self[k]
                break
        try:
            return os.unlink(self.shadowpath(shelf_name))
        except OSError as e:
            if e.errno == errno.ENOENT:
                return 0
            raise TmfsOSError(e.errno)

    def _create_open_common(self, shelf, flags, mode):
        if mode is None:
            mode = 0o666
        try:
            fd = os.open(self.shadowpath(shelf.name), flags, mode=mode)
        except OSError as e:
            if flags & os.O_CREAT:
                if e.errno != errno.EEXIST:
                    raise TmfsOSError(e.errno)
            else:
                raise TmfsOSError(e.errno)
        self[fd] = shelf
        return fd

    def open(self, shelf, flags, mode=None):
        return self._create_open_common(shelf, flags, mode)

    def create(self, shelf, mode=None):
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
        return self._create_open_common(shelf, flags, mode)

    def read(self, shelf_name, length, offset, fd):
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, length)

    def write(self, shelf_name, buf, offset, fd):
        os.lseek(fd, offset, os.SEEK_SET)
        return os.write(fd, buf)

    def truncate(self, shelf, length, fd):
        try:
            if fd:
                assert shelf == self[fd], 'Oops'
            os.truncate(
                fd if fd is not None else self.shadowpath(shelf.name),
                length)
            if fd is not None:
                self[fd].size_bytes = length
            return 0
        except OSError as e:
            raise TmfsOSError(e.errno)

    def release(self, fh):
        shelf = super(self.__class__, self).release(fh)
        os.close(fh)    # I don't think this ever raises....
        return shelf

#--------------------------------------------------------------------------


class shadow_file(shadow_support):
    '''Use one (large) shadow file indexed by "normalized" LZA (ie,
       discontiguous holes in LZA are made smooth for the file.  This
       file lives in the file system of the entity running lfs_fuse.'''

    def __init__(self, args, lfs_globals):
        super(self.__class__, self).__init__(args, lfs_globals)

        (head, tail) = os.path.split(args.shadow_file)

        assert os.path.isdir(head), 'No such directory %s' % head

        try:
            probe = tempfile.TemporaryFile(dir=head)
            probe.close()
        except OSError as e:
            raise RuntimeError('%s is not writeable' % head)

        if os.path.isfile(args.shadow_file):
            fd = os.open(args.shadow_file, os.O_RDWR)
        else:
            fd = os.open(args.shadow_file, os.O_RDWR | os.O_CREAT)
            size = lfs_globals['nvm_bytes_total']
            os.ftruncate(fd, size)

        # Compare node requirements to file size
        statinfo = os.stat(args.shadow_file)
        assert self._mode_rw_file == self._mode_rw_file & statinfo.st_mode, \
            '%s is not RW'
        assert statinfo.st_size >= lfs_globals['nvm_bytes_total']

        self._shadow_fd = fd

    def open(self, shelf, flags, mode=None):
        self[shelf.open_handle] = shelf
        return shelf.open_handle

    def create(self, shelf, mode):
        self[shelf.open_handle] = shelf
        return shelf.open_handle

    def read(self, shelf_name, length, offset, fd):

        if ((offset % self.book_size) + length) <= self.book_size:
            shadow_offset = self.shadow_offset(shelf_name, offset)
            os.lseek(self._shadow_fd, shadow_offset, os.SEEK_SET)
            return os.read(self._shadow_fd, length)

        # Read overlaps books, split into multiple chunks

        buf = b''
        cur_offset = offset
        tot_length = length

        while (tot_length > 0):
            cur_length = min((self.book_size - (cur_offset % self.book_size)),
                             tot_length)
            shadow_offset = self.shadow_offset(shelf_name, cur_offset)

            if shadow_offset == -1:
                break

            os.lseek(self._shadow_fd, shadow_offset, os.SEEK_SET)
            buf += os.read(self._shadow_fd, cur_length)

            if self.verbose > 2:
                print("READ: co = %d, tl = %d, cl = %d, so = %d, bl = %d" % (
                      cur_offset, tot_length, cur_length,
                      shadow_offset, len(buf)))

            offset += cur_length
            cur_offset += cur_length
            tot_length -= cur_length

        return buf

    def write(self, shelf_name, buf, offset, fd):

        if ((offset % self.book_size) + len(buf)) <= self.book_size:
            shadow_offset = self.shadow_offset(shelf_name, offset)
            os.lseek(self._shadow_fd, shadow_offset, os.SEEK_SET)
            return os.write(self._shadow_fd, buf)

        # Write overlaps books, split into multiple chunks

        tbuf = b''
        buf_offset = 0
        cur_offset = offset
        tot_length = len(buf)
        wsize = 0

        while (tot_length > 0):
            cur_length = min((self.book_size - (cur_offset % self.book_size)),
                             tot_length)
            shadow_offset = self.shadow_offset(shelf_name, cur_offset)

            assert shadow_offset != -1, "shadow_offset -1 during write"

            # chop buffer in pieces
            buf_end = buf_offset + cur_length
            tbuf = buf[buf_offset:buf_end]

            os.lseek(self._shadow_fd, shadow_offset, os.SEEK_SET)
            wsize += os.write(self._shadow_fd, tbuf)

            if self.verbose > 2:
                print("WRITE: co = %d, tl = %d, cl = %d, so = %d,"
                      " bl = %d, bo = %d, wsize = %d, be = %d" % (
                          cur_offset, tot_length, cur_length, shadow_offset,
                          len(tbuf), buf_offset, wsize, buf_end))

            offset += cur_length
            cur_offset += cur_length
            tot_length -= cur_length
            buf_offset += cur_length
            tbuf = b''

        return wsize

#--------------------------------------------------------------------------
# Original class did read/write against the resource file by mmapping slices
# against /sys/bus/pci/..../resource2.  mmap in kernel was handled by
# legacy generic fault handles that used read/write.  There was a lot of
# overhead but it was functional against true global shared memory.

# 'class fam' supported true mmap in the kernel against the physical
# address of the IVSHMEM device, but it couldn't do read and write.  Merging
# the two classes (actually, just getxattr() from the previous "class fam")
# gets the best of both worlds.


class shadow_ivshmem(shadow_support):

    def __init__(self, args, lfs_globals):

        super(self.__class__, self).__init__(args, lfs_globals)

        # Retrieve ivshmem information.  Our convention states the first
        # IVSHMEM device found is used as fabric-attached memory.  Parse the
        # first block of lines of lspci -vv for Bus-Device-Function and
        # BAR2 information.

        lspci = getoutput('lspci -vv -d1af4:1110').split('\n')[:11]
        assert lspci[0].endswith('Red Hat, Inc Inter-VM shared memory'), \
            'IVSHMEM device not found'
        bdf = lspci[0].split()[0]
        if self.verbose > 2:
            print('IVSHMEM device at %s used as fabric-attached memory' % bdf)
        mf = '/sys/devices/pci0000:00/0000:%s/resource2' % bdf
        assert (os.path.isfile(mf)), '%s is not a file' % mf

        region2 = [ l for l in lspci if 'Region 2:' in l ][0]
        assert ('(64-bit, prefetchable)' in region2), \
            'IVSHMEM region 2 not found for device %s' % bdf
        self.aperture_base = int(region2.split('Memory at')[1].split()[0], 16)
        assert self.aperture_base, \
            'Could not retrieve base address of IVSHMEM device at %s' % bdf

        # Compare requirements to file size
        statinfo = os.stat(mf)
        assert self._mode_rw_file == self._mode_rw_file & statinfo.st_mode, \
            '%s is not RW' % mf
        assert statinfo.st_size >= lfs_globals['nvm_bytes_total'], \
            'st_size (%d) < nvm_bytes_total (%d)' % \
            (statinfo.st_size, lfs_globals['nvm_bytes_total'])

        # Paranoia check in face of multiple IVSHMEMS: zbridge emulation
        # has firewall table of 32M.  Make sure this is bigger.
        assert statinfo.st_size > 64 * 1 << 20, \
            'IVSHMEM at %s is not big enough, possible collision?' % bdf
        self.aperture_size = statinfo.st_size

        # os.open vs. built-in allows all the low-level stuff I need.
        self._shadow_fd = os.open(mf, os.O_RDWR)
        self._mmap = mmap.mmap(
            self._shadow_fd, 0, prot=mmap.PROT_READ | mmap.PROT_WRITE)

        if self.verbose > 2:
            print('IVSHMEM max offset is 0x%x; physical addresses 0x%x - 0x%x' % (
                  self.aperture_size - 1,
                  self.aperture_base, self.aperture_base + self.aperture_size - 1))

        self.descriptors = DescriptorManagement(args)

    # Single node: no caching.  Multinode might change that?
    def open(self, shelf, flags, mode=None):
        assert isinstance(shelf.open_handle, int), 'Bad handle in shadow open'
        self[shelf.open_handle] = shelf
        return shelf.open_handle

    # Single node: no caching.  Multinode might change that?
    def create(self, shelf, mode):
        assert isinstance(shelf.open_handle, int), 'Bad handle in shadow create'
        self[shelf.open_handle] = shelf     # should be first instance
        return shelf.open_handle

    def read(self, shelf_name, length, offset, fd):

        if ((offset % self.book_size) + length) <= self.book_size:
            shadow_offset = self.shadow_offset(shelf_name, offset)
            self._mmap.seek(shadow_offset, 0)
            return self._mmap.read(length)

        # Read overlaps books, split into multiple chunks

        buf = b''
        cur_offset = offset
        tot_length = length

        while (tot_length > 0):
            cur_length = min((self.book_size - (cur_offset % self.book_size)),
                             tot_length)
            shadow_offset = self.shadow_offset(shelf_name, cur_offset)

            if shadow_offset == -1: break

            self._mmap.seek(shadow_offset, 0)
            buf += self._mmap.read(cur_length)

            if self.verbose > 2:
                print("READ: co = %d, tl = %d, cl = %d, so = %d, bl = %d" % (
                      cur_offset, tot_length, cur_length,
                      shadow_offset, len(buf)))

            offset += cur_length
            cur_offset += cur_length
            tot_length -= cur_length

        return buf

    def write(self, shelf_name, buf, offset, fd):

        if ((offset % self.book_size) + len(buf)) <= self.book_size:
            shadow_offset = self.shadow_offset(shelf_name, offset)
            self._mmap.seek(shadow_offset, 0)
            # write to mmap file always returns "None"
            self._mmap.write(buf)
            return len(buf)

        # Write overlaps books, split into multiple chunks

        tbuf = b''
        buf_offset = 0
        cur_offset = offset
        tot_length = len(buf)
        wsize = 0

        while (tot_length > 0):
            cur_length = min((self.book_size - (cur_offset % self.book_size)),
                             tot_length)
            shadow_offset = self.shadow_offset(shelf_name, cur_offset)

            assert shadow_offset != -1, "shadow_offset -1 during write"

            # chop buffer in pieces
            buf_end = buf_offset + cur_length
            tbuf = buf[buf_offset:buf_end]

            self._mmap.seek(shadow_offset, 0)
            # write to mmap file always returns "None"
            self._mmap.write(tbuf)
            wsize += len(tbuf)

            if self.verbose > 2:
                print("WRITE: co = %d, tl = %d, cl = %d, so = %d,"
                      " bl = %d, bo = %d, wsize = %d, be = %d" % (
                          cur_offset, tot_length, cur_length, shadow_offset,
                          len(tbuf), buf_offset, wsize, buf_end))

            offset += cur_length
            cur_offset += cur_length
            tot_length -= cur_length
            buf_offset += cur_length
            tbuf = b''

        return wsize

    def getxattr(self, shelf_name, xattr):
        # Called during fault handler in kernel, don't die here :-)
        try:
            bos = self[shelf_name].bos
            cmd, comm, pid, offset, userVA = xattr.split(':')
            pid = int(pid)
            offset = int(offset)
            userVA = int(userVA)
            book_num = offset // self.book_size  # (0..n-1)
            book_offset = offset % self.book_size
            if book_num >= len(bos):
                return 'ERROR'
            baseLZA = bos[book_num]['lza']

            eviction = self.descriptors.assign(baseLZA, pid, userVA)
            if eviction is not None:
                # Contains a list of PIDs whose PTEs need to be invalidated
                # over this physical range.
                if self.verbose > 2:
                    print('---> EVICT %s: %s' % (
                        eviction.evictLZA.baseLZA,
                        ','.join(
                            [str(k) for k in eviction.evictLZA.pids.keys()])))

            # FAME physical offset for virtual to physical mapping during fault
            ivshmem_offset = self.shadow_offset(shelf_name, offset)
            if ivshmem_offset == -1:
                return 'ERROR'
            physaddr = self.aperture_base + ivshmem_offset

            # ivshmem does not have real apertures even though calculations
            # are being done.   Mapping is direct.
            data = 'direct:%s:%s' % (physaddr, self.book_size)

            if self.verbose > 3:
                print('Process %s[%d] shelf = %s, offset = %d (0x%x)' % (
                    comm, pid, shelf_name, offset, offset))
                print('shelf book seq=%d, LZA=0x%x -> IG=%d, IGoffset=%d' % (
                    book_num,
                    baseLZA,
                    baseLZA >> 13,
                    baseLZA & ((1 << 13) -1 )))
                print('physaddr = %d (0x%x)' % (physaddr, physaddr))
                print('IVSHMEM backing file offset = %d (0x%x)' % (
                    ivshmem_offset, ivshmem_offset))
                print('data returned to fault handler = %s' % (data))

            return data

        except Exception as e:
            print('!!! ERROR IN FAULT HANDLER: %s' % str(e), file=sys.stderr)
            return ''

#--------------------------------------------------------------------------

class fam(shadow_ivshmem):

    def __init__(self, args, lfs_globals):

        super(shadow_ivshmem, self).__init__(args, lfs_globals)

        self.aperture_base = int(args.fam, 16)

        if self.verbose > 2:
            print('FAM aperture_base = 0x%x' % (self.aperture_base))

        self.descriptors = DescriptorManagement(args)


def the_shadow_knows(args, lfs_globals):
    '''args is command-line arguments from lfs_fuse.py'''
    try:
        if args.shadow_dir:
            return shadow_directory(args, lfs_globals)
        elif args.shadow_file:
            return shadow_file(args, lfs_globals)
        elif args.shadow_ivshmem or args.shadow_apertures:
            return shadow_ivshmem(args, lfs_globals)
        elif args.fam:
            return fam(args, lfs_globals)
        else:
            raise ValueError('Illegal shadow setting "%s"' % args.shadow_dir)
    except Exception as e:
        msg = str(e)
    # seems to be ignored, as is SystemExit
    raise OSError(errno.EINVAL, 'lfs_shadow: %s' % msg)
