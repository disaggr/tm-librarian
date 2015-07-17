#!/usr/bin/python3 -tt

from pdb import set_trace

#########################################################################

class BookShelfStuff(object):      # could become a mixin

    def __init__(self, *args, **kwargs):
        if args:
            assert not kwargs, 'full tuple or kwargs, not both'
            assert len(args) == len(self._ordered_schema), 'bad arg count'
            submitted = dict(zip(self._ordered_schema, args))
            missing = { }
        else:
            submitted = frozenset(kwargs.keys())
            missing = self.__slots__ - submitted
            if True and not self.__slots__.issubset(submitted):
                print('Missing fields "%s"' % (
                    ', '.join(sorted([k for k in missing]))))
            submitted = kwargs
            missing = dict(zip(missing, (0, ) * len(missing)))

        for src in (submitted, missing):
            for k, v in src.items():
                setattr(self, k, v)

    def __eq__(self, other):
        for k in self._ordered_schema:
            if getattr(self, k) != getattr(other, k):
                return False
        return True

    def __str__(self):
        s = [ ]
        for k in self._sorted:
            s.append('{}: {}'.format( k, getattr(self, k)))
        return '\n'.join(s)

    def __getitem__(self, key):    # and now I'm a dict
        return getattr(self, key)

    def tuple(self, *args):
        if not args:
            args = self._ordered_schema
        return tuple([getattr(self, a) for a in args])

#########################################################################

class TMBook(BookShelfStuff):

    _ordered_schema = ( # a little dodgy
        'book_id',
        'node_id',
        'status',
        'attributes',
        'size_bytes',
    )

    _sorted = tuple(sorted(_ordered_schema))

    __slots__ = frozenset((_ordered_schema))

#########################################################################

class TMShelf(BookShelfStuff):

    _ordered_schema = ( # a little dodgy
            'shelf_id',
            'size_bytes',
            'book_count',
            'open_count',
            'c_time',
            'm_time',
    )

    _sorted = tuple(sorted(_ordered_schema))

    __slots__ = frozenset((_ordered_schema))

#########################################################################
# Support testing

def mem_db_init(cur):
    table_create = """
            CREATE TABLE IF NOT EXISTS books (
            book_id INT PRIMARY KEY,
            node_id INT,
            status INT,
            attributes INT,
            size_bytes INT
            )
    """
    cur.execute(table_create)

    table_create = """
            CREATE TABLE IF NOT EXISTS shelves (
            shelf_id INT PRIMARY KEY,
            size_bytes INT,
            book_count INT,
            open_count INT,
            c_time REAL,
            m_time REAL
            )
    """
    cur.execute(table_create)

    table_create = """
            CREATE TABLE IF NOT EXISTS books_on_shelf (
            shelf_id INT,
            book_id INT,
            seq_num INT
            )
    """
    cur.execute(table_create)

    cur.commit()

#########################################################################

if __name__ == '__main__':

    from librariansql import SQLiteCursor

    cur = SQLiteCursor()    # no args == :memory:

    set_trace()
    book1 = TMBook()
    print(book1)

    shelf1 = TMShelf()
    print(shelf1)

    mem_db_init(cur)
    fields = cur.schema('books')
    assert set(fields) == set(TMBook._ordered_schema), 'TMBook oopsie'
    fields = cur.schema('shelves')
    assert set(fields) == set(TMShelf._ordered_schema), 'TMShelfoopsie'

    sql = '''INSERT INTO books VALUES (?, ?, ?, ?, ?)'''
    set_trace()
    cur.execute(sql, book1.tuple())
    cur.commit()
    print(cur.rowcount, "row inserted") # only after updates, not SELECT

    # ways to build objects

    sql = 'SELECT * FROM Books LIMIT 1'
    tmp = cur.execute(sql).fetchone()
    plagiarize = TMBook(*tmp)
    print(book1 == plagiarize)

    pass

    cur.close()

    raise SystemExit(0)
