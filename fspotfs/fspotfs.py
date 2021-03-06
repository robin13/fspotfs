#!/usr/bin/python
"""
Copyright (C) 2009  Matias Aguirre <matiasaguirre@gmail.com>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import os, sys, stat, errno, fuse, time, sqlite3
from urllib import unquote
from optparse import OptionParser, OptionError
from os.path import basename, dirname, join, isfile, isabs


fuse.fuse_python_api = (0, 2)

DESCRIPTION        = 'F-Spot FUSE Filesystem'
FSPOT_DB_FILE      = 'f-spot/photos.db'
FSPOT_DB_VERSION   = 17.1 # database version supported
DEFAULT_MOUNTPOINT = join(os.environ['HOME'], '.photos')

###
# SQL sentences

# get F-Spot database version
DB_VERSION_SQL = """SELECT data FROM meta
                    WHERE name = "F-Spot Database Version"
                    LIMIT 1"""

# get real path parts for photo in tag
FILE_SQL = """SELECT replace(p.base_uri, 'file://', ''),
                     p.filename
              FROM photo_tags pt
              LEFT JOIN photos p
                ON p.id = pt.photo_id
              WHERE pt.tag_id = ? AND p.filename = ?
              LIMIT 1"""

# get photos for tag excluding photos in sub-tags
TAG_PHOTOS = """SELECT p.filename
                FROM photo_tags pt
                LEFT JOIN photos p
                    ON p.id = pt.photo_id
                WHERE pt.tag_id = ? AND
                      pt.photo_id NOT IN (SELECT DISTINCT pt2.photo_id
                                          FROM photo_tags pt2
                                          WHERE pt2.tag_id IN %(in_items)s)"""

# photos for leaf tag
LEAF_PHOTOS = """SELECT p.filename
                 FROM photo_tags pt
                 LEFT JOIN photos p
                    ON p.id = pt.photo_id
                 WHERE pt.tag_id = ?"""

# All photos
ALL_PHOTOS = 'SELECT filename FROM photos'

# return id for tag name
TAG_ID = 'SELECT id FROM tags WHERE name = ? LIMIT 1'

# tag names
TAG_NAMES = 'SELECT name FROM tags'

# sub-tag names
SUBTAG_NAMES = 'SELECT name FROM tags WHERE category_id = ?'
        
# sub-tag ids
SUBTAG_IDS = 'SELECT id FROM tags WHERE category_id = ?'

# Startup time
GLOBAL_TIME = int(time.time())

###
# Internal cache to make queries faster
_cache = {}

def cls_cached(prefix):
    """Decorator that holds an application cache that stores return
    values to make queries faster."""
    def decorator(fn):
        def wrapper(slf, *args, **kwargs):
            global _cache
            # generate key
            key = '_'.join([prefix] + list(map(str, args)) +
                           filter(None, kwargs.values()))
            if key not in _cache:
                _cache[key] = fn(slf, *args, **kwargs)
            return _cache[key]
        return wrapper
    return decorator

def with_cursor(fn):
    """Wraps a function that needs a cursor."""
    def wrapper(db_path, sql, *params):
        conn = sqlite3.connect(db_path)
        if conn:
            cur = conn.cursor()
            cur.execute('PRAGMA temp_store = MEMORY');
            cur.execute('PRAGMA synchronous = OFF');
            result = fn(cur, db_path, sql, *params)
            cur.close()
            return result
    return wrapper


@with_cursor
def query_multiple(cur, db_path, sql, *params):
    """Executes SQL query."""
    cur.execute(sql, params)
    return cur.fetchall()

@with_cursor
def query_one(cur, db_path, sql, *params):
    """Execute sql and return just one row."""
    cur.execute(sql, params)
    return cur.fetchone()

def prepare_in(sql, items):
    """Prepares an sql statement with an IN clause.
    The sql *must* have an 'in_items' placeholder."""
    return sql % {'in_items': '(' + ', '.join('?' for item in items) + ')'}

###
# Stats for FUSE implementation

class BaseStat(fuse.Stat):
    """Base Stat class. Sets atime, mtime and ctime to a dummy
    global value (time when application started running)."""
    def __init__(self, *args, **kwargs):
        """Init atime, mtime and ctime."""
        super(BaseStat, self).__init__(*args, **kwargs)
        self.st_atime = self.st_mtime = self.st_ctime = GLOBAL_TIME


class DirStat(BaseStat):
    """Directory stat"""
    def __init__(self, *args, **kwargs):
        super(DirStat, self).__init__(*args, **kwargs)
        self.st_mode = stat.S_IFDIR | 0755
        self.st_nlink = 2


class ImageLinkStat(BaseStat):
    """Link to Image stat"""
    def __init__(self, path, *args, **kwargs):
        super(ImageLinkStat, self).__init__(*args, **kwargs)
        self.st_mode = stat.S_IFREG|stat.S_IFLNK|0644
        self.st_nlink = 0
        os_stat = os.stat(path)
        self.st_size = os_stat.st_size if os_stat else 0


###
# FUSE F-Spot FS
class FSpotFS(fuse.Fuse):
    """F-Spot FUSE filesystem implementation. Just readonly support
    at the moment"""
    def __init__(self, db_path, repeated, *args, **kwargs):
        self.db_path = db_path
        self.repeated = repeated
        super(FSpotFS, self).__init__(*args, **kwargs)

    def query(self, sql, *params):
        """Executes SQL query."""
        params = tuple(str(param) for param in params)
        return query_multiple(self.db_path, sql, *params)

    def query_one(self, sql, *params):
        """Executes SQL query."""
        params = tuple(str(param) for param in params)
        return query_one(self.db_path, sql, *params)

    @cls_cached('tag_childs')
    def tag_childs(self, parent):
        """Return ids of sub-tags of parent tag. Goes deep in the
        tag hierarchy returning second-level, and deeper subtags."""
        # first-level subtags
        result = set([str(tag[0]) for tag in self.query(SUBTAG_IDS, parent)])
        # second-level subtags and deeper
        next_level = set(reduce(lambda l1, l2: l1 + l2,
                                [self.tag_childs(tid) for tid in result],
                                []))
        return list(result) + list(next_level)

    @cls_cached('tag_names')
    def tag_names(self, parent=None):
        """Return tag names for parent or all tag names."""
        if parent is not None:
            tags = self.query(SUBTAG_NAMES, parent)
        else:
            tags = self.query(TAG_NAMES)
        return [tag[0] for tag in tags]

    @cls_cached('encoded_tags_cache')
    def encoded_tag_names(self):
        """Return encoded tag names."""
        return [i.encode('utf-8') for i in self.tag_names()]

    @cls_cached('tag_to_id')
    def tag_to_id(self, name):
        """Return tag if for tag name or None."""
        try:
            return self.query_one(TAG_ID, name)[0]
        except (TypeError, KeyError):
            pass

    @cls_cached('file_names')
    def file_names(self, tag=None):
        """Return photo names tagged as 'tag' or all photos if not tag,
        sub-tags are excluded if self.repeated is false."""
        if tag is not None:
            if not self.repeated:
                children = self.tag_childs(tag)
                if children: # get photos for tag avoiding sub-tags
                    files = self.query(prepare_in(TAG_PHOTOS, children),
                                       tag, *children)
                else: # get tag photos for current no-parent tag
                    files = self.query(LEAF_PHOTOS, tag)
            else: # get tag photos not ignoring repeated
                files = self.query(LEAF_PHOTOS, tag)
        else: # get all photos
            files = self.query(ALL_PHOTOS)
        return [file[0] for file in files]

    @cls_cached('encoded_file_names')
    def encoded_file_names(self):
        """Encode file names."""
        return [i.encode('utf-8') for i in self.file_names()]

    @cls_cached('link_path')
    def link_path(self, tag, name):
        """Return path to filename."""
        row = self.query_one(FILE_SQL, self.tag_to_id(tag), name)
        try:
            uri, filename = row
            return unquote(join(uri, filename)).encode('utf-8')
        except (TypeError, IndexError):
            pass
        return ''

    def is_dir(self, path):
        """Check if path is a directory in f-spot."""
        return path in ('.', '..', '/') or \
               basename(path) in self.encoded_tag_names()

    def is_file(self, path):
        """Check if path is a file in f-spot."""
        return basename(path) in self.encoded_file_names()

    @cls_cached('getattr')
    def getattr(self, path):
        """Getattr handler."""
        if self.is_dir(path):
            return DirStat()
        elif self.is_file(path):
            fname = basename(path)
            tag = basename(dirname(path))
            if fname in self.file_names(self.tag_to_id(tag)):
                return ImageLinkStat(self.link_path(tag, fname))
        return -errno.ENOENT

    @cls_cached('readlink')
    def readlink(self, path):
        """Readlink handler."""
        return self.link_path(basename(dirname(path)), basename(path))

    @cls_cached('access')
    def access(self, path, offset):
        """Check file access."""
        # Access granted by default at the moment, unless the file does
        # not exists
        if self.is_dir(path) or \
           self.is_file(path) and \
           basename(path) in \
           self.file_names(self.tag_to_id(basename(dirname(path)))):
            return 0
        return -errno.EINVAL

    def readdir(self, path, offset):
        """Readdier handler."""
        # Yields items returned by _readdir method
        for item in self._readdir(path, offset):
            yield item

    @cls_cached('readdir')
    def _readdir(self, path, offset):
        parent = 0 if path == '/' else self.tag_to_id(basename(path))

        dirs = [fuse.Direntry(r.encode('utf-8'))
                    for r in self.tag_names(parent)]
        dirs.sort(key=lambda x: x.name)

        type = stat.S_IFREG | stat.S_IFLNK
        files = [fuse.Direntry(i.encode('utf-8'), type=type)
                    for i in self.file_names(parent)]
        files.sort(key=lambda x: x.name)

        return [fuse.Direntry('.'), fuse.Direntry('..')] + dirs + files


def run():
    """Parse commandline options and run server"""
    parser = OptionParser(usage='%prog [options]', description=DESCRIPTION)
    parser.add_option('-d', '--fsdb', action='store', type='string',
                      dest='fsdb', default='',
                      help='Path to F-Spot sqlite database.')
    parser.add_option('-m', '--mount', action='store', type='string',
                      dest='mountpoint', default=DEFAULT_MOUNTPOINT,
                      help='Mountpoint path (default %s)' % DEFAULT_MOUNTPOINT)
    parser.add_option('-r', '--repeated', action='store_true', dest='repeated',
                      help='Show re-tagged images in the same family tree' \
                           ' (default False)')
    parser.add_option('-v', '--dbversion', action='store', type='string',
                      dest='dbversion', default=FSPOT_DB_VERSION,
                      help='F-Spot database schema version to use' \
                           ' (default v%s)' % FSPOT_DB_VERSION)
    try:
        opts, args = parser.parse_args()
    except OptionError, e: # Invalid option
        print >>sys.stderr, str(e)
        parser.print_help()
        sys.exit(1)

    # override F-Spot database path
    if opts.fsdb:
        fspot_db = opts.fsdb
        if not isabs(fspot_db):
            fspot_db = join(os.environ['HOME'], fspot_db)
    elif 'XDG_CONFIG_HOME' in os.environ:
        # build F-Spot database path with XDG enviroment values
        fspot_db = join(os.environ['XDG_CONFIG_HOME'], FSPOT_DB_FILE)
    else:
        # build F-Spot database HOME enviroment value
        fspot_db = join(os.environ['HOME'], '.config', FSPOT_DB_FILE)

    if not isfile(fspot_db):
        print >>sys.stderr, 'File "%s" not found' % fspot_db
        parser.print_help()
        sys.exit(1)

    # check database schema compatibility
    assert float(query_one(fspot_db, DB_VERSION_SQL)[0]) == opts.dbversion

    args = fuse.FuseArgs()
    args.mountpoint = opts.mountpoint
    FSpotFS(fspot_db, opts.repeated, fuse_args=args).main() # run server


if __name__ == '__main__':
    run()
