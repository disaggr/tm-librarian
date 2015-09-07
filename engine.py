#!/usr/bin/python3 -tt
#---------------------------------------------------------------------------
# Librarian engine module
#---------------------------------------------------------------------------

import errno
import uuid
import time
import math
import sys
from pdb import set_trace

from book_shelf_bos import TMBook, TMShelf, TMBos
from cmdproto import LibrarianCommandProtocol
from genericobj import GenericObject

class LibrarianCommandEngine(object):

    @staticmethod
    def argparse_extend(parser):
        pass

    _book_size_bytes = 0
    _nvm_bytes_total = 0  # read from DB

    @property
    def book_size_bytes(self):
        return self._book_size_bytes

    @property
    def nvm_bytes_total(self):
        return self._nvm_bytes_total

    @classmethod
    def _nbooks(cls, nbytes):
        return int(math.ceil(float(nbytes) / float(cls._book_size_bytes)))

    def cmd_version(self, cmdict):
        """ Return librarian version
            In (dict)---
                None
            Out (dict) ---
                librarian version
        """
        return self.db.get_globals(only='version')

    def cmd_get_fs_stats(self, cmdict):
        """ Return globals
            In (dict)---
                None
            Out (dict) ---
                librarian version
        """
        return self.db.get_globals()

    def cmd_create_shelf(self, cmdict):
        """ Create a new shelf
            In (dict)---
                name
            Out (dict) ---
                shelf data
        """
        # POSIX: create if not existent, else open
        try:
            shelf = self.cmd_open_shelf(cmdict)
            return shelf
        except Exception as e:
            pass
        self.errno = errno.EINVAL
        shelf = TMShelf(cmdict)
        self.db.create_shelf(shelf)
        return self.cmd_open_shelf(cmdict)  # Does the handle thang

    def cmd_get_shelf(self, cmdict, match_id=False):
        """ List a given shelf.
            In (dict)---
                name
                optional flag to force a match on id (ie, already open)
            Out (TMShelf object) ---
                TMShelf object
        """
        self.errno = errno.EINVAL
        shelf = TMShelf(cmdict)
        assert shelf.name, 'Missing shelf name'
        if match_id:
            shelf.matchfields = ('name', 'id')
        else:
            shelf.matchfields = ('name', )
        shelf = self.db.get_shelf(shelf)
        if shelf is None:
            self.errno = errno.ENOENT   # FIXME: raise OSError instead?
            raise AssertionError('no such shelf %s' % cmdict['name'])
        # consistency checks
        self.errno = errno.EBADF
        assert self._nbooks(shelf.size_bytes) == shelf.book_count, (
            '%s size metadata mismatch' % shelf.name)
        self.errno = errno.ESTALE
        # FIXME: calculate open count and compare against shelf handle/id
        return shelf

    def cmd_list_shelves(self, cmdict):
        '''Returns a list.'''
        return self.db.get_shelf_all()

    def cmd_open_shelf(self, cmdict):
        """ Open a shelf for access by a node.
            In (dict)---
                name
            Out ---
                TMShelf object
        """
        shelf = self.cmd_get_shelf(cmdict)  # may raise ENOENT
        self.db.modify_opened_shelves(shelf, 'get', cmdict['context'])
        return shelf

    def cmd_close_shelf(self, cmdict):
        """ Close a shelf against access by a node.
            In (dict)---
                shelf_id, name, handle
            Out (dict) ---
                TMShelf object
        """
        shelf = TMShelf(cmdict)
        shelf = self.db.modify_opened_shelves(shelf, 'put', cmdict['context'])
        return shelf

    def _list_shelf_books(self, shelf):
        self.errno = errno.EBADF
        assert shelf.id, '%s not open' % shelf.name
        bos = self.db.get_bos_by_shelf_id(shelf.id)

        # consistency checks.  Leave them both as different paths may
        # have been followed to retrieve the passed-in shelf.
        self.errno = errno.EREMOTEIO
        assert len(bos) == shelf.book_count, (
            '%s book count mismatch' % shelf.name)
        assert self._nbooks(shelf.size_bytes) == shelf.book_count, (
            '%s size metadata mismatch' % shelf.name)
        return bos

    def cmd_list_shelf_books(self, cmdict):
        shelf = self.cmd_get_shelf(cmdict)
        return self._list_shelf_books(shelf)

    def _set_book_alloc(self, book_id, newalloc):
        book = self.db.get_book_by_id(book_id)
        self.errno = errno.ENOENT
        assert book, 'Book lookup failed'
        self.errno = errno.EUCLEAN
        msg = 'Book allocation %d -> %d' % (book.allocated, newalloc)
        if newalloc == TMBook.ALLOC_INUSE:
            assert book.allocated == TMBook.ALLOC_FREE, msg
        elif newalloc == TMBook.ALLOC_ZOMBIE:
            assert book.allocated == TMBook.ALLOC_INUSE, msg
        elif newalloc == TMBook.ALLOC_FREE:
            assert book.allocated == TMBook.ALLOC_ZOMBIE, msg
        else:
            raise RuntimeError('Bad book allocation %d' % newalloc)
        book.allocated = newalloc
        book.matchfields = 'allocated'
        book = self.db.modify_book(book)
        self.errno = errno.ENOENT
        assert book, 'Book allocation change to %d failed' % newalloc
        return book

    def cmd_destroy_shelf(self, cmdict):
        """ For a shelf, zombify books (mark for zeroing) and remove xattrs
            In (dict)---
                shelf
                node
            Out (dict) ---
                shelf data
        """
        self.errno = errno.EBUSY
        shelf = self.cmd_get_shelf(cmdict)
        assert not self.db.open_count(shelf), '%s has active opens' % shelf.name
        bos = self._list_shelf_books(shelf)
        xattrs = self.db.list_xattrs(shelf)
        for thisbos in bos:
            self.db.delete_bos(thisbos)
            _ = self._set_book_alloc(thisbos.book_id, TMBook.ALLOC_ZOMBIE)
        for xattr in xattrs:
            self.db.delete_xattr(shelf, xattr)
        return self.db.delete_shelf(shelf, commit=True)

    def cmd_kill_zombie_books(self, cmdict):
        '''repl_client command to "zero" zombie books.  Needs work.'''
        node_id = cmdict['context']['node_id']
        zombies = self.db.get_book_by_node(
                node_id, TMBook.ALLOC_ZOMBIE, 9999)
        for book in zombies:
            _ = self._set_book_alloc(book.id, TMBook.ALLOC_FREE)
        self.db.commit()
        return None

    def cmd_log_zero(self, cmdict):
        '''Positive response from LFS daemon. NRFPT'''
        self.errno = errno.EINVAL
        node_id = cmdict['context']['node_id']
        for id in cmdict['ids']:
            book = self.db.get_book_by_id(id)
            assert book.node_id == node_id, 'Attempted off-node zeroing'
            book = self._set_book_alloc(id, TMBook.ALLOC_FREE)
        self.db.commit()
        return None

    def cmd_resize_shelf(self, cmdict):
        """ Resize given shelf to new size in bytes.
            In (dict)---
                name
                id
                size_bytes
            Out (dict) ---
                shelf data
        """
        shelf = self.cmd_get_shelf(cmdict, match_id=True)

        bos = self._list_shelf_books(shelf)
        self.errno = errno.EREMOTEIO
        assert len(bos) == shelf.book_count, (
            '%s book count mismatch' % shelf.name)

        new_size_bytes = int(cmdict['size_bytes'])
        self.errno = errno.EINVAL
        assert new_size_bytes >= 0, 'Bad size'
        new_book_count = self._nbooks(new_size_bytes)
        if bos:
            seqs = [ b.seq_num for b in bos ]
            self.errno = errno.EBADFD
            assert set(seqs) == set(range(1, shelf.book_count + 1)), (
                'Corrupt BOS sequence progression for %s' % shelf.name)

        # Can I leave real early?
        if new_size_bytes == shelf.size_bytes:
            return shelf
        shelf.size_bytes = new_size_bytes

        # How about a little early?
        if new_book_count == shelf.book_count:
            shelf.matchfields = 'size_bytes'
            shelf = self.db.modify_shelf(shelf, commit=True)
            return shelf

        books_needed = new_book_count - shelf.book_count
        node_id = cmdict['context']['node_id']
        if books_needed > 0:
            seq_num = shelf.book_count
            freebooks = self.db.get_book_by_node(
                node_id, TMBook.ALLOC_FREE, books_needed)
            self.errno = errno.ENOSPC
            assert len(freebooks) == books_needed, (
                'out of space on node %d for "%s"' % (node_id, shelf.name))
            for book in freebooks: # Mark book in use and create BOS entry
                book = self._set_book_alloc(book.id, TMBook.ALLOC_INUSE)
                seq_num += 1
                thisbos = TMBos(
                    shelf_id=shelf.id, book_id=book.id, seq_num=seq_num)
                thisbos = self.db.create_bos(thisbos)
        elif books_needed < 0:
            books_2bdel = -books_needed    # it all reads so much better
            self.errno = errno.EREMOTEIO
            assert len(bos) >= books_2bdel, 'Book removal problem'
            while books_2bdel > 0:
                thisbos = bos.pop()
                self.db.delete_bos(thisbos)
                _ = self._set_book_alloc(thisbos.book_id, TMBook.ALLOC_ZOMBIE)
                books_2bdel -= 1
        else:
            self.db.rollback()
            self.errno = errno.EREMOTEIO
            raise RuntimeError('Bad code path in cmd_resize_shelf()')

        shelf.book_count = new_book_count
        shelf.matchfields = ('size_bytes', 'book_count')
        shelf = self.db.modify_shelf(shelf, commit=True)
        return shelf

    def cmd_get_shelf_zaddr(cmd_data, cmdict):
        """
            In (dict)---
                ?
            Out (dict) ---
                ?
        """
        raise NotImplementedError

    def cmd_get_book(cmd_data, cmdict):
        """ List a given book
            In (dict)---
                book_id
            Out (dict) ---
                book data
        """
        set_trace()
        book_id = cmd_data["book_id"]
        resp = db.get_book_by_id(book_id)
        # todo: fail if book does not exist
        recvd = dict(zip(book_columns, resp))
        return recvd

    def cmd_get_xattr(self, cmdict):
        """ Retrieve name/value pair for an extendend attribute of a shelf.
            In (dict)---
                name
                id
                xattr
            Out (dict) ---
                value
        """
        # Zombie is okay, they should be cleared.
        shelf = self.cmd_get_shelf(cmdict)
        value = self.db.get_xattr(shelf, cmdict['xattr'])
        return { 'value': value }

    def cmd_list_xattrs(self, cmdict):
        """ Retrieve names of all extendend attributes of a shelf.
            In (dict)---
                name
            Out (list) ---
                value
        """
        shelf = self.cmd_get_shelf(cmdict)
        value = self.db.list_xattrs(shelf)
        return { 'value': value }

    def cmd_set_xattr(self, cmdict):
        """ Set/update name/value pair for an extended attribute of a shelf.
            In (dict)---
                name
                id
                xattr
                value
            Out (dict) ---
                None or raise error
        """
        # XATTR_CREATE/REPLACE option is not being set on the other side
        # FIXME: can this only be done on an open shelf?
        shelf = self.cmd_get_shelf(cmdict)
        if self.db.get_xattr(shelf, cmdict['xattr'], exists_only=True):
            return self.db.modify_xattr(
                shelf, cmdict['xattr'], cmdict['value'])
        return self.db.create_xattr(
            shelf, cmdict['xattr'], cmdict['value'])

    def cmd_set_am_time(self, cmdict):
        """ Set access and modified times, usually of a shelf but
            maybe also the librarian itself.  For now we ignore atime.
            In (dict)---
                name
                atime
                mtime
            Out (list) ---
                None or error
        """
        shelf = self.cmd_get_shelf(cmdict)
        shelf.matchfields = 'mtime' # special case
        shelf.mtime = cmdict['mtime']
        self.db.modify_shelf(shelf, commit=True)
        return None

    def cmd_send_OOB(self, cmdict):
        '''In general any command that creates an OOB condition
           needs to attach the OOB resolution for all clients.
           This API is merely for testing purposes.'''
        return {
            'value': cmdict['context']['node_id'],
            'OOBmsg': cmdict['msg']
        }

    #######################################################################

    _commands = None

    def __init__(self, backend, optargs=None, cooked=False):
        try:
            self.db = backend
            globals = self.db.get_globals()
            (self.__class__._book_size_bytes,
             self.__class__._nvm_bytes_total) = (
                globals.book_size_bytes,
                globals.nvm_bytes_total
            )

            # Skip 'cmd_' prefix
            self.__class__._commands = dict( [ (name[4:], func)
                        for (name, func) in self.__class__.__dict__.items() if
                            name.startswith('cmd_')
                        ]
            )
            self._cooked = cooked   # return style: raw = dict, cooked = obj
        except Exception as e:
            raise RuntimeError('FATAL INITIALIZATION ERROR: %s' % str(e))

    def __call__(self, cmdict):
        try:
            self.errno = 0
            context = cmdict['context']
            command = self._commands[cmdict['command']]
        except KeyError as e:
            # This comment might go better in the module that imports json.
            # From StackOverflow: NULL is not zero. It's not a value, per se:
            # it is a value outside the domain of the variable's type,
            # indicating missing or unknown data.  There is only one way to
            # represent null in JSON. Per the specs (RFC 4627 and json.org):
            # 2.1.  Values.  A JSON value MUST be an object, array, number,
            # or string, OR one of the following three literal names:
            # false null true
            # Python's json handler turns None into 'null' and vice verse.
            errmsg = 'engine failed lookup on "%s"' % str(e)
            print('!' * 20, errmsg, file=sys.stderr)
            # Higher-order internal error
            return { 'errmsg': errmsg, 'errno': errno.ENOSYS }, None

        try:
            errmsg = '' # High-level internal errors, not LFS state errors
            self.errno = 0
            ret = OOBmsg = None
            ret = command(self, cmdict)
        except AssertionError as e:     # consistency checks
            errmsg = str(e)
        except (AttributeError, RuntimeError) as e: # idiot checks
            errmsg = 'INTERNAL ERROR @ %s[%d]: %s' % (
                self.__class__.__name__, sys.exc_info()[2].tb_lineno,str(e))
        except Exception as e:          # the Unknown Idiot
            errmsg = 'UNEXPECTED ERROR @ %s[%d]: %s' % (
                self.__class__.__name__, sys.exc_info()[2].tb_lineno,str(e))
        if errmsg: # Looks better _cooked
            return { 'errmsg': errmsg, 'errno': self.errno }, None

        if isinstance(ret, dict):
            OOBmsg = ret.get('OOBmsg', None)
            if OOBmsg is not None:
                OOBmsg = { 'OOBmsg': OOBmsg }
                del ret['OOBmsg']
        if self._cooked:    # for self-test
            return ret, OOBmsg

        # Create a dict to which context will be added
        if type(ret) in (dict, str) or ret is None:
            value = { 'value': ret }
        elif isinstance(ret, list):
            value = { 'value': [ r.dict for r in ret ] }
        else:
            value = { 'value': ret.dict }
        value['context'] = context  # has sequence
        return value, OOBmsg

    @property
    def commandset(self):
        return tuple(sorted(self._commands.keys()))

###########################################################################
# Use LCP to construct command dictionaries from fixed data.  Those
# dictionaries are what would be "received" from real clients.
# Exercises are written against an SQLite3 database so create it
# beforehand with book_register.py.

if __name__ == '__main__':


    import os
    from argparse import Namespace # the result of an argparse sequence.
    from pprint import pprint

    from backend_sqlite3 import LibrarianDBackendSQLite3

    def pp(recvd, data):
        print('Command:', dict(recvd))
        print('DB action results:')
        pprint(data)
        print()

    umask = os.umask(0) # The Pythonic way to get the current umask.
    os.umask(umask)     # FIXME: move this into...somewhere?
    context = {
        'uid': os.geteuid(),
        'gid': os.getegid(),
        'pid': os.getpid(),
        'umask': umask,
        'node_id': 1,
    }

    lcp = LibrarianCommandProtocol(context)
    print(lcp.commandset)

    # For self test, look at prettier results than dictionaries.
    args = Namespace(db_file=sys.argv[1])
    lce = LibrarianCommandEngine(
                    LibrarianDBackendSQLite3(args),
                    cooked=True)
    print(lce.commandset)

    print()
    print('Engine missing:',set(lcp.commandset) - set(lce.commandset))
    print('Engine extras: ',set(lce.commandset) - set(lcp.commandset))

    recvd = lcp('version')
    version = lce(recvd)
    pp(recvd, version)

    for name in ('xyzzy', 'shelf22', 'coke', 'pepsi'):
        recvd = lcp('create_shelf', name=name)
        try:
            shelf = lce(recvd)   # only works on fresh DB
        except Exception as e:
            shelf = str(e)
            if shelf.startswith('INTERNAL ERROR'):
                set_trace()
                raise e
        pp(recvd, shelf)

    # Two ways to get started
    name = 'xyzzy'
    recvd = lcp('get_shelf', name=name)
    shelf = lce(recvd)
    pp(recvd, shelf)

    recvd = lcp('get_shelf', shelf)    # a shelf object with 'name'
    shelf = lce(recvd)
    pp(recvd, shelf)

    recvd = lcp('list_shelves')
    shelves = lce(recvd)
    assert len(shelves) >= 4, 'not good'
    pp(recvd, shelf)

    # Some FS operations (in FuSE) first require an open shelf.  The
    # determination of that state is simple: does it have a shelf id?
    recvd = lcp('open_shelf', shelf)
    shelf = lce(recvd)
    pp(recvd, shelf)
    if shelf is None:
        raise SystemExit('Shelf ' + name + ' has disappeared (open)')

    shelf.size_bytes = (70 * lce.book_size_bytes)
    recvd = lcp('resize_shelf', shelf)
    shelf = lce(recvd)
    pp(recvd, shelf)
    if shelf is None or hasattr(shelf, 'errmsgr'):
        raise SystemExit('Shelf ' + name + ' problems (initial size)')

    shelf.size_bytes = (50 * lce.book_size_bytes)
    recvd = lcp('resize_shelf', shelf)
    shelf = lce(recvd)
    pp(recvd, shelf)
    if shelf is None or hasattr(shelf, 'errmsg'):
        raise SystemExit('Shelf ' + name + ' problems (resize down)')

    recvd = lcp('close_shelf', shelf)
    shelf = lce(recvd)
    pp(recvd, shelf)
    if shelf is None or hasattr(shelf, 'errmsg'):
        raise SystemExit('Shelf ' + name + ' problems (close shelf)')

    # destroy shelf is just based on the name
    recvd = lcp('destroy_shelf', shelf)
    shelf = lce(recvd)
    pp(recvd, shelf)
    if shelf is None or hasattr(shelf, 'errmsg'):
        raise SystemExit('Shelf ' + name + ' problems (destroy shelf)')

    raise SystemExit(0)
