#
# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2015 Douglas S. Blank <doug.blank@gmail.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
#

#------------------------------------------------------------------------
#
# Python Modules
#
#------------------------------------------------------------------------
import pickle
import base64
import time
import re
import os
import logging
import shutil
import bisect

#------------------------------------------------------------------------
#
# Gramps Modules
#
#------------------------------------------------------------------------
import gramps
from gramps.gen.const import GRAMPS_LOCALE as glocale
_ = glocale.translation.gettext
from gramps.gen.db import (DbReadBase, DbWriteBase, DbTxn, DbUndo,
                           KEY_TO_NAME_MAP, KEY_TO_CLASS_MAP, 
                           CLASS_TO_KEY_MAP, TXNADD, TXNUPD, TXNDEL)
from gramps.gen.utils.callback import Callback
from gramps.gen.updatecallback import UpdateCallback
from gramps.gen.db.dbconst import *
from gramps.gen.db import (PERSON_KEY,
                           FAMILY_KEY,
                           CITATION_KEY,
                           SOURCE_KEY,
                           EVENT_KEY,
                           MEDIA_KEY,
                           PLACE_KEY,
                           REPOSITORY_KEY,
                           NOTE_KEY,
                           TAG_KEY)

from gramps.gen.utils.id import create_id
from gramps.gen.lib.researcher import Researcher
from gramps.gen.lib import (Tag, MediaObject, Person, Family, Source, Citation, Event,
                            Place, Repository, Note, NameOriginType)
from gramps.gen.lib.genderstats import GenderStats

import dbapi_support

_LOG = logging.getLogger(DBLOGNAME)

_SIGBASE = ('person', 'family', 'source', 'event', 'media',
            'place', 'repository', 'reference', 'note', 'tag', 'citation')

def touch(fname, mode=0o666, dir_fd=None, **kwargs):
    ## After http://stackoverflow.com/questions/1158076/implement-touch-using-python
    flags = os.O_CREAT | os.O_APPEND
    with os.fdopen(os.open(fname, flags=flags, mode=mode, dir_fd=dir_fd)) as f:
        os.utime(f.fileno() if os.utime in os.supports_fd else fname,
                 dir_fd=None if os.supports_fd else dir_fd, **kwargs)

class DBAPIUndo(DbUndo):
    def __init__(self, grampsdb, path):
        super(DBAPIUndo, self).__init__(grampsdb)
        self.undodb = []

    def open(self, value=None):
        """
        Open the backing storage.  Needs to be overridden in the derived
        class.
        """
        pass

    def close(self):
        """
        Close the backing storage.  Needs to be overridden in the derived
        class.
        """        
        pass

    def append(self, value):
        """
        Add a new entry on the end.  Needs to be overridden in the derived
        class.
        """        
        self.undodb.append(value)

    def __getitem__(self, index):
        """
        Returns an entry by index number.  Needs to be overridden in the
        derived class.
        """        
        return self.undodb[index]

    def __setitem__(self, index, value):
        """
        Set an entry to a value.  Needs to be overridden in the derived class.
        """           
        self.undodb[index] = value

    def __len__(self):
        """
        Returns the number of entries.  Needs to be overridden in the derived
        class.
        """         
        return len(self.undodb)

    def _redo(self, update_history):
        """
        Access the last undone transaction, and revert the data to the state 
        before the transaction was undone.
        """
        txn = self.redoq.pop()
        self.undoq.append(txn)
        transaction = txn
        db = self.db
        subitems = transaction.get_recnos()

        # Process all records in the transaction
        for record_id in subitems:
            (key, trans_type, handle, old_data, new_data) = \
                pickle.loads(self.undodb[record_id])

            if key == REFERENCE_KEY:
                self.undo_reference(new_data, handle, self.mapbase[key])
            else:
                self.undo_data(new_data, handle, self.mapbase[key],
                                    db.emit, _SIGBASE[key])
        # Notify listeners
        if db.undo_callback:
            db.undo_callback(_("_Undo %s")
                                   % transaction.get_description())

        if db.redo_callback:
            if self.redo_count > 1:
                new_transaction = self.redoq[-2]
                db.redo_callback(_("_Redo %s")
                                   % new_transaction.get_description())
            else:
                db.redo_callback(None)       

        if update_history and db.undo_history_callback:
            db.undo_history_callback()
        return True        

    def _undo(self, update_history):
        """
        Access the last committed transaction, and revert the data to the 
        state before the transaction was committed.
        """
        txn = self.undoq.pop()
        self.redoq.append(txn)
        transaction = txn
        db = self.db
        subitems = transaction.get_recnos(reverse=True)

        # Process all records in the transaction
        for record_id in subitems:
            (key, trans_type, handle, old_data, new_data) = \
                    pickle.loads(self.undodb[record_id])

            if key == REFERENCE_KEY:
                self.undo_reference(old_data, handle, self.mapbase[key])
            else:
                self.undo_data(old_data, handle, self.mapbase[key],
                                db.emit, _SIGBASE[key])
        # Notify listeners
        if db.undo_callback:
            if self.undo_count > 0:
                db.undo_callback(_("_Undo %s")
                                   % self.undoq[-1].get_description())
            else:
                db.undo_callback(None)    

        if db.redo_callback:
            db.redo_callback(_("_Redo %s")
                                   % transaction.get_description())

        if update_history and db.undo_history_callback:
            db.undo_history_callback()
        return True

class Environment(object):
    """
    Implements the Environment API.
    """
    def __init__(self, db):
        self.db = db

    def txn_begin(self):
        return DBAPITxn("DBAPIDb Transaction", self.db)

    def txn_checkpoint(self):
        pass

class Table(object):
    """
    Implements Table interface.
    """
    def __init__(self, db, table_name, funcs=None):
        self.db = db
        self.table_name = table_name
        if funcs:
            self.funcs = funcs
        else:
            self.funcs = db._tables[table_name]

    def cursor(self):
        """
        Returns a Cursor for this Table.
        """
        return self.funcs["cursor_func"]()

    def put(self, key, data, txn=None):
        self.funcs["add_func"](data, txn)

class Map(object):
    """
    Implements the map API for person_map, etc.
    
    Takes a Table() as argument.
    """
    def __init__(self, table, 
                 keys_func="handles_func", 
                 contains_func="has_handle_func",
                 raw_func="raw_func",
                 *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.table = table
        self.keys_func = keys_func
        self.contains_func = contains_func
        self.raw_func = raw_func
        self.txn = DBAPITxn("Dummy transaction", db=self.table.db, batch=True)

    def keys(self):
        return self.table.funcs[self.keys_func]()

    def values(self):
        return self.table.funcs["cursor_func"]()

    def __contains__(self, key):
        return self.table.funcs[self.contains_func](key)

    def __getitem__(self, key):
        if self.table.funcs[self.contains_func](key):
            return self.table.funcs[self.raw_func](key)

    def __setitem__(self, key, value):
        """
        This is only done in a low-level raw import.

        value: serialized object
        key: bytes key (ignored in this implementation)
        """
        obj = self.table.funcs["class_func"].create(value)
        self.table.funcs["commit_func"](obj, self.txn)

    def __len__(self):
        return self.table.funcs["count_func"]()

    def delete(self, key):
        self.table.funcs["del_func"](key, self.txn)

class MetaCursor(object):
    def __init__(self):
        pass
    def __enter__(self):
        return self
    def __iter__(self):
        return self.__next__()
    def __next__(self):
        yield None
    def __exit__(self, *args, **kwargs):
        pass
    def iter(self):
        yield None
    def first(self):
        self._iter = self.__iter__()
        return self.next()
    def next(self):
        try:
            return next(self._iter)
        except:
            return None
    def close(self):
        pass

class Cursor(object):
    def __init__(self, map):
        self.map = map
        self._iter = self.__iter__()
    def __enter__(self):
        return self
    def __iter__(self):
        for item in self.map.keys():
            yield (bytes(item, "utf-8"), self.map[item])
    def __next__(self):
        try:
            return self._iter.__next__()
        except StopIteration:
            return None
    def __exit__(self, *args, **kwargs):
        pass
    def iter(self):
        for item in self.map.keys():
            yield (bytes(item, "utf-8"), self.map[item])
    def first(self):
        self._iter = self.__iter__()
        try:
            return next(self._iter)
        except:
            return
    def next(self):
        try:
            return next(self._iter)
        except:
            return
    def close(self):
        pass

class TreeCursor(Cursor):

    def __init__(self, db, map):
        """
        """
        self.db = db
        Cursor.__init__(self, map)

    def __iter__(self):
        """
        Iterator
        """
        handles = self.db.get_place_handles(sort_handles=True)
        for handle in handles:
            yield (bytes(handle, "utf-8"), self.db._get_raw_place_data(handle))

class Bookmarks(object):
    def __init__(self, default=[]):
        self.handles = list(default)

    def set(self, handles):
        self.handles = list(handles)

    def get(self):
        return self.handles

    def append(self, handle):
        self.handles.append(handle)

    def append_list(self, handles):
        self.handles += handles

    def remove(self, handle):
        self.handles.remove(handle)

    def pop(self, item):
        return self.handles.pop(item)

    def insert(self, pos, item):
        self.handles.insert(pos, item)

    def close(self):
        del self.handles
    

class DBAPITxn(DbTxn):
    def __init__(self, message, db, batch=False):
        DbTxn.__init__(self, message, db, batch)

    def get(self, key, default=None, txn=None, **kwargs):
        """
        Returns the data object associated with key
        """
        if txn and key in txn:
            return txn[key]
        else:
            return None

    def put(self, handle, new_data, txn):
        """
        """
        txn[handle] = new_data

class DBAPI(DbWriteBase, DbReadBase, UpdateCallback, Callback):
    """
    A Gramps Database Backend. This replicates the grampsdb functions.
    """
    __signals__ = dict((obj+'-'+op, signal)
                       for obj in
                       ['person', 'family', 'event', 'place',
                        'source', 'citation', 'media', 'note', 'repository', 'tag']
                       for op, signal in zip(
                               ['add',   'update', 'delete', 'rebuild'],
                               [(list,), (list,),  (list,),   None]
                       )
                   )
    
    # 2. Signals for long operations
    __signals__.update(('long-op-'+op, signal) for op, signal in zip(
        ['start',  'heartbeat', 'end'],
        [(object,), None,       None]
        ))

    # 3. Special signal for change in home person
    __signals__['home-person-changed'] = None

    # 4. Signal for change in person group name, parameters are
    __signals__['person-groupname-rebuild'] = (str, str)

    __callback_map = {}

    def __init__(self, directory=None):
        DbReadBase.__init__(self)
        DbWriteBase.__init__(self)
        Callback.__init__(self)
        self._tables['Person'].update(
            {
                "handle_func": self.get_person_from_handle, 
                "gramps_id_func": self.get_person_from_gramps_id,
                "class_func": Person,
                "cursor_func": self.get_person_cursor,
                "handles_func": self.get_person_handles,
                "add_func": self.add_person,
                "commit_func": self.commit_person,
                "iter_func": self.iter_people,
                "ids_func": self.get_person_gramps_ids,
                "has_handle_func": self.has_handle_for_person,
                "has_gramps_id_func": self.has_gramps_id_for_person,
                "count": self.get_number_of_people,
                "raw_func": self._get_raw_person_data,
                "raw_id_func": self._get_raw_person_from_id_data,
                "del_func": self.remove_person,
            })
        self._tables['Family'].update(
            {
                "handle_func": self.get_family_from_handle, 
                "gramps_id_func": self.get_family_from_gramps_id,
                "class_func": Family,
                "cursor_func": self.get_family_cursor,
                "handles_func": self.get_family_handles,
                "add_func": self.add_family,
                "commit_func": self.commit_family,
                "iter_func": self.iter_families,
                "ids_func": self.get_family_gramps_ids,
                "has_handle_func": self.has_handle_for_family,
                "has_gramps_id_func": self.has_gramps_id_for_family,
                "count": self.get_number_of_families,
                "raw_func": self._get_raw_family_data,
                "raw_id_func": self._get_raw_family_from_id_data,
                "del_func": self.remove_family,
            })
        self._tables['Source'].update(
            {
                "handle_func": self.get_source_from_handle, 
                "gramps_id_func": self.get_source_from_gramps_id,
                "class_func": Source,
                "cursor_func": self.get_source_cursor,
                "handles_func": self.get_source_handles,
                "add_func": self.add_source,
                "commit_func": self.commit_source,
                "iter_func": self.iter_sources,
                "ids_func": self.get_source_gramps_ids,
                "has_handle_func": self.has_handle_for_source,
                "has_gramps_id_func": self.has_gramps_id_for_source,
                "count": self.get_number_of_sources,
                "raw_func": self._get_raw_source_data,
                "raw_id_func": self._get_raw_source_from_id_data,
                "del_func": self.remove_source,
                })
        self._tables['Citation'].update(
            {
                "handle_func": self.get_citation_from_handle, 
                "gramps_id_func": self.get_citation_from_gramps_id,
                "class_func": Citation,
                "cursor_func": self.get_citation_cursor,
                "handles_func": self.get_citation_handles,
                "add_func": self.add_citation,
                "commit_func": self.commit_citation,
                "iter_func": self.iter_citations,
                "ids_func": self.get_citation_gramps_ids,
                "has_handle_func": self.has_handle_for_citation,
                "has_gramps_id_func": self.has_gramps_id_for_citation,
                "count": self.get_number_of_citations,
                "raw_func": self._get_raw_citation_data,
                "raw_id_func": self._get_raw_citation_from_id_data,
                "del_func": self.remove_citation,
            })
        self._tables['Event'].update(
            {
                "handle_func": self.get_event_from_handle, 
                "gramps_id_func": self.get_event_from_gramps_id,
                "class_func": Event,
                "cursor_func": self.get_event_cursor,
                "handles_func": self.get_event_handles,
                "add_func": self.add_event,
                "commit_func": self.commit_event,
                "iter_func": self.iter_events,
                "ids_func": self.get_event_gramps_ids,
                "has_handle_func": self.has_handle_for_event,
                "has_gramps_id_func": self.has_gramps_id_for_event,
                "count": self.get_number_of_events,
                "raw_func": self._get_raw_event_data,
                "raw_id_func": self._get_raw_event_from_id_data,
                "del_func": self.remove_event,
            })
        self._tables['Media'].update(
            {
                "handle_func": self.get_object_from_handle, 
                "gramps_id_func": self.get_object_from_gramps_id,
                "class_func": MediaObject,
                "cursor_func": self.get_media_cursor,
                "handles_func": self.get_media_object_handles,
                "add_func": self.add_object,
                "commit_func": self.commit_media_object,
                "iter_func": self.iter_media_objects,
                "ids_func": self.get_media_gramps_ids,
                "has_handle_func": self.has_handle_for_media,
                "has_gramps_id_func": self.has_gramps_id_for_media,
                "count": self.get_number_of_media_objects,
                "raw_func": self._get_raw_media_data,
                "raw_id_func": self._get_raw_media_from_id_data,
                "del_func": self.remove_object,
            })
        self._tables['Place'].update(
            {
                "handle_func": self.get_place_from_handle, 
                "gramps_id_func": self.get_place_from_gramps_id,
                "class_func": Place,
                "cursor_func": self.get_place_cursor,
                "handles_func": self.get_place_handles,
                "add_func": self.add_place,
                "commit_func": self.commit_place,
                "iter_func": self.iter_places,
                "ids_func": self.get_place_gramps_ids,
                "has_handle_func": self.has_handle_for_place,
                "has_gramps_id_func": self.has_gramps_id_for_place,
                "count": self.get_number_of_places,
                "raw_func": self._get_raw_place_data,
                "raw_id_func": self._get_raw_place_from_id_data,
                "del_func": self.remove_place,
            })
        self._tables['Repository'].update(
            {
                "handle_func": self.get_repository_from_handle, 
                "gramps_id_func": self.get_repository_from_gramps_id,
                "class_func": Repository,
                "cursor_func": self.get_repository_cursor,
                "handles_func": self.get_repository_handles,
                "add_func": self.add_repository,
                "commit_func": self.commit_repository,
                "iter_func": self.iter_repositories,
                "ids_func": self.get_repository_gramps_ids,
                "has_handle_func": self.has_handle_for_repository,
                "has_gramps_id_func": self.has_gramps_id_for_repository,
                "count": self.get_number_of_repositories,
                "raw_func": self._get_raw_repository_data,
                "raw_id_func": self._get_raw_repository_from_id_data,
                "del_func": self.remove_repository,
            })
        self._tables['Note'].update(
            {
                "handle_func": self.get_note_from_handle, 
                "gramps_id_func": self.get_note_from_gramps_id,
                "class_func": Note,
                "cursor_func": self.get_note_cursor,
                "handles_func": self.get_note_handles,
                "add_func": self.add_note,
                "commit_func": self.commit_note,
                "iter_func": self.iter_notes,
                "ids_func": self.get_note_gramps_ids,
                "has_handle_func": self.has_handle_for_note,
                "has_gramps_id_func": self.has_gramps_id_for_note,
                "count": self.get_number_of_notes,
                "raw_func": self._get_raw_note_data,
                "raw_id_func": self._get_raw_note_from_id_data,
                "del_func": self.remove_note,
            })
        self._tables['Tag'].update(
            {
                "handle_func": self.get_tag_from_handle, 
                "gramps_id_func": None,
                "class_func": Tag,
                "cursor_func": self.get_tag_cursor,
                "handles_func": self.get_tag_handles,
                "add_func": self.add_tag,
                "commit_func": self.commit_tag,
                "has_handle_func": self.has_handle_for_tag,
                "iter_func": self.iter_tags,
                "count": self.get_number_of_tags,
                "raw_func": self._get_raw_tag_data,
                "del_func": self.remove_tag,
            })
        self.set_save_path(directory)
        # skip GEDCOM cross-ref check for now:
        self.set_feature("skip-check-xref", True)
        self.set_feature("skip-import-additions", True)
        self.readonly = False
        self.db_is_open = True
        self.name_formats = []
        # Bookmarks:
        self.bookmarks = Bookmarks()
        self.family_bookmarks = Bookmarks()
        self.event_bookmarks = Bookmarks()
        self.place_bookmarks = Bookmarks()
        self.citation_bookmarks = Bookmarks()
        self.source_bookmarks = Bookmarks()
        self.repo_bookmarks = Bookmarks()
        self.media_bookmarks = Bookmarks()
        self.note_bookmarks = Bookmarks()
        self.set_person_id_prefix('I%04d')
        self.set_object_id_prefix('O%04d')
        self.set_family_id_prefix('F%04d')
        self.set_citation_id_prefix('C%04d')
        self.set_source_id_prefix('S%04d')
        self.set_place_id_prefix('P%04d')
        self.set_event_id_prefix('E%04d')
        self.set_repository_id_prefix('R%04d')
        self.set_note_id_prefix('N%04d')
        # ----------------------------------
        self.undodb = None
        self.id_trans  = DBAPITxn("ID Transaction", self)
        self.fid_trans = DBAPITxn("FID Transaction", self)
        self.pid_trans = DBAPITxn("PID Transaction", self)
        self.cid_trans = DBAPITxn("CID Transaction", self)
        self.sid_trans = DBAPITxn("SID Transaction", self)
        self.oid_trans = DBAPITxn("OID Transaction", self)
        self.rid_trans = DBAPITxn("RID Transaction", self)
        self.nid_trans = DBAPITxn("NID Transaction", self)
        self.eid_trans = DBAPITxn("EID Transaction", self)
        self.cmap_index = 0
        self.smap_index = 0
        self.emap_index = 0
        self.pmap_index = 0
        self.fmap_index = 0
        self.lmap_index = 0
        self.omap_index = 0
        self.rmap_index = 0
        self.nmap_index = 0
        self.env = Environment(self)
        self.person_map = Map(Table(self, "Person"))
        self.person_id_map = Map(Table(self, "Person"),
                                 keys_func="ids_func",
                                 contains_func="has_gramps_id_func",
                                 raw_func="raw_id_func")
        self.family_map = Map(Table(self, "Family"))
        self.family_id_map = Map(Table(self, "Family"),
                                 keys_func="ids_func",
                                 contains_func="has_gramps_id_func",
                                 raw_func="raw_id_func")
        self.place_map  = Map(Table(self, "Place"))
        self.place_id_map = Map(Table(self, "Place"),
                                keys_func="ids_func",
                                contains_func="has_gramps_id_func",
                                raw_func="raw_id_func")
        self.citation_map = Map(Table(self, "Citation"))
        self.citation_id_map = Map(Table(self, "Citation"),
                                   keys_func="ids_func",
                                   contains_func="has_gramps_id_func",
                                   raw_func="raw_id_func")
        self.source_map = Map(Table(self, "Source"))
        self.source_id_map = Map(Table(self, "Source"),
                                 keys_func="ids_func",
                                 contains_func="has_gramps_id_func",
                                 raw_func="raw_id_func")
        self.repository_map  = Map(Table(self, "Repository"))
        self.repository_id_map = Map(Table(self, "Repository"),
                                     keys_func="ids_func",
                                     contains_func="has_gramps_id_func",
                                     raw_func="raw_id_func")
        self.note_map = Map(Table(self, "Note"))
        self.note_id_map = Map(Table(self, "Note"),
                               keys_func="ids_func",
                               contains_func="has_gramps_id_func",
                               raw_func="raw_id_func")
        self.media_map  = Map(Table(self, "Media"))
        self.media_id_map = Map(Table(self, "Media"),
                                keys_func="ids_func",
                                contains_func="has_gramps_id_func",
                                raw_func="raw_id_func")
        self.event_map  = Map(Table(self, "Event"))
        self.event_id_map = Map(Table(self, "Event"), 
                                keys_func="ids_func",
                                contains_func="has_gramps_id_func",
                                raw_func="raw_id_func")
        self.tag_map  = Map(Table(self, "Tag"))
        self.metadata   = Map(Table(self, "Metadata", funcs={"cursor_func": lambda: MetaCursor()}))
        self.undo_callback = None
        self.redo_callback = None
        self.undo_history_callback = None
        self.modified   = 0
        self.txn = DBAPITxn("DBAPI Transaction", self)
        self.transaction = None
        self.abort_possible = False
        self._bm_changes = 0
        self.has_changed = False
        self.surname_list = []
        self.genderStats = GenderStats() # can pass in loaded stats as dict
        self.owner = Researcher()
        if directory:
            self.load(directory)

    def version_supported(self):
        """Return True when the file has a supported version."""
        return True

    def get_table_names(self):
        """Return a list of valid table names."""
        return list(self._tables.keys())

    def get_table_metadata(self, table_name):
        """Return the metadata for a valid table name."""
        if table_name in self._tables:
            return self._tables[table_name]
        return None

    def transaction_begin(self, transaction):
        """
        Transactions are handled automatically by the db layer.
        """
        self.transaction = transaction
        return transaction

    def transaction_commit(self, txn):
        """
        Executed after a batch operation.
        """
        self.dbapi.commit()
        self.transaction = None
        msg = txn.get_description()
        self.undodb.commit(txn, msg)
        self.__after_commit(txn)
        txn.clear()
        self.has_changed = True

    def __after_commit(self, transaction):
        """
        Post-transaction commit processing
        """
        if transaction.batch:
            self.env.txn_checkpoint()
            # Only build surname list after surname index is surely back
            self.build_surname_list()

        # Reset callbacks if necessary
        if transaction.batch or not len(transaction):
            return
        if self.undo_callback:
            self.undo_callback(_("_Undo %s") % transaction.get_description())
        if self.redo_callback:
            self.redo_callback(None)
        if self.undo_history_callback:
            self.undo_history_callback()            

    def transaction_abort(self, txn):
        """
        Executed after a batch operation abort.
        """
        self.dbapi.rollback()
        self.transaction = None
        txn.clear()
        txn.first = None
        txn.last = None
        self.__after_commit(txn)

    @staticmethod
    def _validated_id_prefix(val, default):
        if isinstance(val, str) and val:
            try:
                str_ = val % 1
            except TypeError:           # missing conversion specifier
                prefix_var = val + "%d"
            except ValueError:          # incomplete format
                prefix_var = default+"%04d"
            else:
                prefix_var = val        # OK as given
        else:
            prefix_var = default+"%04d" # not a string or empty string
        return prefix_var

    @staticmethod
    def __id2user_format(id_pattern):
        """
        Return a method that accepts a Gramps ID and adjusts it to the users
        format.
        """
        pattern_match = re.match(r"(.*)%[0 ](\d+)[diu]$", id_pattern)
        if pattern_match:
            str_prefix = pattern_match.group(1)
            nr_width = int(pattern_match.group(2))
            def closure_func(gramps_id):
                if gramps_id and gramps_id.startswith(str_prefix):
                    id_number = gramps_id[len(str_prefix):]
                    if id_number.isdigit():
                        id_value = int(id_number, 10)
                        #if len(str(id_value)) > nr_width:
                        #    # The ID to be imported is too large to fit in the
                        #    # users format. For now just create a new ID,
                        #    # because that is also what happens with IDs that
                        #    # are identical to IDs already in the database. If
                        #    # the problem of colliding import and already
                        #    # present IDs is solved the code here also needs
                        #    # some solution.
                        #    gramps_id = id_pattern % 1
                        #else:
                        gramps_id = id_pattern % id_value
                return gramps_id
        else:
            def closure_func(gramps_id):
                return gramps_id
        return closure_func

    def set_person_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Person ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as I%d or I%04d.
        """
        self.person_prefix = self._validated_id_prefix(val, "I")
        self.id2user_format = self.__id2user_format(self.person_prefix)

    def set_citation_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Citation ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as C%d or C%04d.
        """
        self.citation_prefix = self._validated_id_prefix(val, "C")
        self.cid2user_format = self.__id2user_format(self.citation_prefix)
            
    def set_source_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Source ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as S%d or S%04d.
        """
        self.source_prefix = self._validated_id_prefix(val, "S")
        self.sid2user_format = self.__id2user_format(self.source_prefix)
            
    def set_object_id_prefix(self, val):
        """
        Set the naming template for GRAMPS MediaObject ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as O%d or O%04d.
        """
        self.mediaobject_prefix = self._validated_id_prefix(val, "O")
        self.oid2user_format = self.__id2user_format(self.mediaobject_prefix)

    def set_place_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Place ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as P%d or P%04d.
        """
        self.place_prefix = self._validated_id_prefix(val, "P")
        self.pid2user_format = self.__id2user_format(self.place_prefix)

    def set_family_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Family ID values. The string is
        expected to be in the form of a simple text string, or in a format
        that contains a C/Python style format string using %d, such as F%d
        or F%04d.
        """
        self.family_prefix = self._validated_id_prefix(val, "F")
        self.fid2user_format = self.__id2user_format(self.family_prefix)

    def set_event_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Event ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as E%d or E%04d.
        """
        self.event_prefix = self._validated_id_prefix(val, "E")
        self.eid2user_format = self.__id2user_format(self.event_prefix)

    def set_repository_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Repository ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as R%d or R%04d.
        """
        self.repository_prefix = self._validated_id_prefix(val, "R")
        self.rid2user_format = self.__id2user_format(self.repository_prefix)

    def set_note_id_prefix(self, val):
        """
        Set the naming template for GRAMPS Note ID values. 
        
        The string is expected to be in the form of a simple text string, or 
        in a format that contains a C/Python style format string using %d, 
        such as N%d or N%04d.
        """
        self.note_prefix = self._validated_id_prefix(val, "N")
        self.nid2user_format = self.__id2user_format(self.note_prefix)

    def __find_next_gramps_id(self, prefix, map_index, trans):
        """
        Helper function for find_next_<object>_gramps_id methods
        """
        index = prefix % map_index
        while trans.get(str(index), txn=self.txn) is not None:
            map_index += 1
            index = prefix % map_index
        map_index += 1
        return (map_index, index)
        
    def find_next_person_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Person object based off the 
        person ID prefix.
        """
        self.pmap_index, gid = self.__find_next_gramps_id(self.person_prefix,
                                          self.pmap_index, self.id_trans)
        return gid

    def find_next_place_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Place object based off the 
        place ID prefix.
        """
        self.lmap_index, gid = self.__find_next_gramps_id(self.place_prefix,
                                          self.lmap_index, self.pid_trans)
        return gid

    def find_next_event_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Event object based off the 
        event ID prefix.
        """
        self.emap_index, gid = self.__find_next_gramps_id(self.event_prefix,
                                          self.emap_index, self.eid_trans)
        return gid

    def find_next_object_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a MediaObject object based
        off the media object ID prefix.
        """
        self.omap_index, gid = self.__find_next_gramps_id(self.mediaobject_prefix,
                                          self.omap_index, self.oid_trans)
        return gid

    def find_next_citation_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Citation object based off the 
        citation ID prefix.
        """
        self.cmap_index, gid = self.__find_next_gramps_id(self.citation_prefix,
                                          self.cmap_index, self.cid_trans)
        return gid

    def find_next_source_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Source object based off the 
        source ID prefix.
        """
        self.smap_index, gid = self.__find_next_gramps_id(self.source_prefix,
                                          self.smap_index, self.sid_trans)
        return gid

    def find_next_family_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Family object based off the 
        family ID prefix.
        """
        self.fmap_index, gid = self.__find_next_gramps_id(self.family_prefix,
                                          self.fmap_index, self.fid_trans)
        return gid

    def find_next_repository_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Respository object based 
        off the repository ID prefix.
        """
        self.rmap_index, gid = self.__find_next_gramps_id(self.repository_prefix,
                                          self.rmap_index, self.rid_trans)
        return gid

    def find_next_note_gramps_id(self):
        """
        Return the next available GRAMPS' ID for a Note object based off the 
        note ID prefix.
        """
        self.nmap_index, gid = self.__find_next_gramps_id(self.note_prefix,
                                          self.nmap_index, self.nid_trans)
        return gid

    def get_mediapath(self):
        return self.get_metadata("media-path", "")

    def set_mediapath(self, mediapath):
        return self.set_metadata("media-path", mediapath)

    def get_metadata(self, key, default=[]):
        """
        Get an item from the database.
        """
        self.dbapi.execute("SELECT value FROM metadata WHERE setting = ?;", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])
        elif default == []:
            return []
        else:
            return default

    def set_metadata(self, key, value):
        """
        key: string
        value: item, will be serialized here
        """
        self.dbapi.execute("SELECT * FROM metadata WHERE setting = ?;", [key])
        row = self.dbapi.fetchone()
        if row:
            self.dbapi.execute("UPDATE metadata SET value = ? WHERE setting = ?;", 
                               [pickle.dumps(value), key])
        else:
            self.dbapi.execute("INSERT INTO metadata (setting, value) VALUES (?, ?);", 
                               [key, pickle.dumps(value)])
        self.dbapi.commit()

    def set_default_person_handle(self, handle):
        self.set_metadata("default-person-handle", handle)
        self.emit('home-person-changed')

    def get_name_group_keys(self):
        self.dbapi.execute("SELECT name FROM name_group ORDER BY name;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_name_group_mapping(self, key):
        self.dbapi.execute("SELECT grouping FROM name_group WHERE name = ?;", 
                                 [key])
        row = self.dbapi.fetchone()
        if row:
            return row[0]
        else:
            return key

    def get_person_handles(self, sort_handles=False):
        if sort_handles:
            self.dbapi.execute("SELECT handle FROM person ORDER BY order_by;")
        else:
            self.dbapi.execute("SELECT handle FROM person;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_family_handles(self):
        self.dbapi.execute("SELECT handle FROM family;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_event_handles(self):
        self.dbapi.execute("SELECT handle FROM event;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_citation_handles(self, sort_handles=False):
        if sort_handles:
            self.dbapi.execute("SELECT handle FROM citation ORDER BY order_by;")
        else:
            self.dbapi.execute("SELECT handle FROM citation;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_source_handles(self, sort_handles=False):
        if sort_handles:
            self.dbapi.execute("SELECT handle FROM source ORDER BY order_by;")
        else:
            self.dbapi.execute("SELECT handle from source;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_place_handles(self, sort_handles=False):
        if sort_handles:
            self.dbapi.execute("SELECT handle FROM place ORDER BY order_by;")
        else:
            self.dbapi.execute("SELECT handle FROM place;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_repository_handles(self):
        self.dbapi.execute("SELECT handle FROM repository;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_media_object_handles(self, sort_handles=False):
        if sort_handles:
            self.dbapi.execute("SELECT handle FROM media ORDER BY order_by;")
        else:
            self.dbapi.execute("SELECT handle FROM media;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_note_handles(self):
        self.dbapi.execute("SELECT handle FROM note;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_tag_handles(self, sort_handles=False):
        if sort_handles:
            self.dbapi.execute("SELECT handle FROM tag ORDER BY order_by;")
        else:
            self.dbapi.execute("SELECT handle FROM tag;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_event_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        event = None
        if handle in self.event_map:
            event = Event.create(self._get_raw_event_data(handle))
        return event

    def get_family_from_handle(self, handle): 
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        family = None
        if handle in self.family_map:
            family = Family.create(self._get_raw_family_data(handle))
        return family

    def get_repository_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        repository = None
        if handle in self.repository_map:
            repository = Repository.create(self._get_raw_repository_data(handle))
        return repository

    def get_person_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        person = None
        if handle in self.person_map:
            person = Person.create(self._get_raw_person_data(handle))
        return person

    def get_place_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        place = None
        if handle in self.place_map:
            place = Place.create(self._get_raw_place_data(handle))
        return place

    def get_citation_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        citation = None
        if handle in self.citation_map:
            citation = Citation.create(self._get_raw_citation_data(handle))
        return citation

    def get_source_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        source = None
        if handle in self.source_map:
            source = Source.create(self._get_raw_source_data(handle))
        return source

    def get_note_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        note = None
        if handle in self.note_map:
            note = Note.create(self._get_raw_note_data(handle))
        return note

    def get_object_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        media = None
        if handle in self.media_map:
            media = MediaObject.create(self._get_raw_media_data(handle))
        return media

    def get_tag_from_handle(self, handle):
        if isinstance(handle, bytes):
            handle = str(handle, "utf-8")
        tag = None
        if handle in self.tag_map:
            tag = Tag.create(self._get_raw_tag_data(handle))
        return tag

    def get_default_person(self):
        handle = self.get_default_handle()
        if handle:
            return self.get_person_from_handle(handle)
        else:
            return None

    def iter_people(self):
        return (Person.create(data[1]) for data in self.get_person_cursor())

    def iter_person_handles(self):
        self.dbapi.execute("SELECT handle FROM person;")
        row = self.dbapi.fetchone()
        while row:
            yield row[0]
            row = self.dbapi.fetchone()

    def iter_families(self):
        return (Family.create(data[1]) for data in self.get_family_cursor())

    def iter_family_handles(self):
        self.dbapi.execute("SELECT handle FROM family;")
        row = self.dbapi.fetchone()
        while row:
            yield row[0]
            row = self.dbapi.fetchone()

    def get_tag_from_name(self, name):
        self.dbapi.execute("""select handle from tag where order_by = ?;""",
                                 [self._order_by_tag_key(name)])
        row = self.dbapi.fetchone()
        if row:
            self.get_tag_from_handle(row[0])
        return None

    def get_person_from_gramps_id(self, gramps_id):
        if gramps_id in self.person_id_map:
            return Person.create(self.person_id_map[gramps_id])
        return None

    def get_family_from_gramps_id(self, gramps_id):
        if gramps_id in self.family_id_map:
            return Family.create(self.family_id_map[gramps_id])
        return None

    def get_citation_from_gramps_id(self, gramps_id):
        if gramps_id in self.citation_id_map:
            return Citation.create(self.citation_id_map[gramps_id])
        return None

    def get_source_from_gramps_id(self, gramps_id):
        if gramps_id in self.source_id_map:
            return Source.create(self.source_id_map[gramps_id])
        return None

    def get_event_from_gramps_id(self, gramps_id):
        if gramps_id in self.event_id_map:
            return Event.create(self.event_id_map[gramps_id])
        return None

    def get_media_from_gramps_id(self, gramps_id):
        if gramps_id in self.media_id_map:
            return MediaObject.create(self.media_id_map[gramps_id])
        return None

    def get_place_from_gramps_id(self, gramps_id):
        if gramps_id in self.place_id_map:
            return Place.create(self.place_id_map[gramps_id])
        return None

    def get_repository_from_gramps_id(self, gramps_id):
        if gramps_id in self.repository_id_map:
            return Repository.create(self.repository_id_map[gramps_id])
        return None

    def get_note_from_gramps_id(self, gramps_id):
        if gramps_id in self.note_id_map:
            return Note.create(self.note_id_map[gramps_id])
        return None

    def get_number_of_people(self):
        self.dbapi.execute("SELECT count(handle) FROM person;")
        row = self.dbapi.fetchone()
        return row[0]

    def get_number_of_events(self):
        self.dbapi.execute("SELECT count(handle) FROM event;")
        row = self.dbapi.fetchone()
        return row[0]

    def get_number_of_places(self):
        self.dbapi.execute("SELECT count(handle) FROM place;")
        row = self.dbapi.fetchone()
        return row[0]

    def get_number_of_tags(self):
        self.dbapi.execute("SELECT count(handle) FROM tag;")
        row = self.dbapi.fetchone()
        return row[0]

    def get_number_of_families(self):
        self.dbapi.execute("SELECT count(handle) FROM family;")
        row = self.dbapi.fetchone()
        return row[0]

    def get_number_of_notes(self):
        self.dbapi.execute("SELECT count(handle) FROM note;")
        row = self.dbapi.fetchone()
        return row[0]

    def get_number_of_citations(self):
        self.dbapi.execute("SELECT count(handle) FROM citation;")
        row = self.dbapi.fetchone()
        return row[0]

    def get_number_of_sources(self):
        self.dbapi.execute("SELECT count(handle) FROM source;")
        row = self.dbapi.fetchone()
        return row[0]

    def get_number_of_media_objects(self):
        self.dbapi.execute("SELECT count(handle) FROM media;")
        row = self.dbapi.fetchone()
        return row[0]

    def get_number_of_repositories(self):
        self.dbapi.execute("SELECT count(handle) FROM repository;")
        row = self.dbapi.fetchone()
        return row[0]

    def get_place_cursor(self):
        return Cursor(self.place_map)

    def get_place_tree_cursor(self, *args, **kwargs):
        return TreeCursor(self, self.place_map)

    def get_person_cursor(self):
        return Cursor(self.person_map)

    def get_family_cursor(self):
        return Cursor(self.family_map)

    def get_event_cursor(self):
        return Cursor(self.event_map)

    def get_note_cursor(self):
        return Cursor(self.note_map)

    def get_tag_cursor(self):
        return Cursor(self.tag_map)

    def get_repository_cursor(self):
        return Cursor(self.repository_map)

    def get_media_cursor(self):
        return Cursor(self.media_map)

    def get_citation_cursor(self):
        return Cursor(self.citation_map)

    def get_source_cursor(self):
        return Cursor(self.source_map)

    def has_gramps_id(self, obj_key, gramps_id):
        key2table = {
            PERSON_KEY:     self.person_id_map, 
            FAMILY_KEY:     self.family_id_map, 
            SOURCE_KEY:     self.source_id_map, 
            CITATION_KEY:   self.citation_id_map, 
            EVENT_KEY:      self.event_id_map, 
            MEDIA_KEY:      self.media_id_map, 
            PLACE_KEY:      self.place_id_map, 
            REPOSITORY_KEY: self.repository_id_map, 
            NOTE_KEY:       self.note_id_map, 
            }
        return gramps_id in key2table[obj_key]

    def has_person_handle(self, handle):
        return handle in self.person_map

    def has_family_handle(self, handle):
        return handle in self.family_map

    def has_citation_handle(self, handle):
        return handle in self.citation_map

    def has_source_handle(self, handle):
        return handle in self.source_map

    def has_repository_handle(self, handle):
        return handle in self.repository_map

    def has_note_handle(self, handle):
        return handle in self.note_map

    def has_place_handle(self, handle):
        return handle in self.place_map

    def has_event_handle(self, handle):
        return handle in self.event_map

    def has_tag_handle(self, handle):
        return handle in self.tag_map

    def has_object_handle(self, handle):
        return handle in self.media_map

    def has_name_group_key(self, key):
        self.dbapi.execute("SELECT grouping FROM name_group WHERE name = ?;", 
                                 [key])
        row = self.dbapi.fetchone()
        return True if row else False

    def set_name_group_mapping(self, name, grouping):
        self.dbapi.execute("SELECT * FROM name_group WHERE name = ?;", 
                                 [name])
        row = self.dbapi.fetchone()
        if row:
            self.dbapi.execute("DELETE FROM name_group WHERE name = ?;", 
                                     [name])
        self.dbapi.execute("INSERT INTO name_group (name, grouping) VALUES(?, ?);",
                                 [name, grouping])
        self.dbapi.commit()

    def get_raw_person_data(self, handle):
        if handle in self.person_map:
            return self.person_map[handle]
        return None

    def get_raw_family_data(self, handle):
        if handle in self.family_map:
            return self.family_map[handle]
        return None

    def get_raw_citation_data(self, handle):
        if handle in self.citation_map:
            return self.citation_map[handle]
        return None

    def get_raw_source_data(self, handle):
        if handle in self.source_map:
            return self.source_map[handle]
        return None

    def get_raw_repository_data(self, handle):
        if handle in self.repository_map:
            return self.repository_map[handle]
        return None

    def get_raw_note_data(self, handle):
        if handle in self.note_map:
            return self.note_map[handle]
        return None

    def get_raw_place_data(self, handle):
        if handle in self.place_map:
            return self.place_map[handle]
        return None

    def get_raw_object_data(self, handle):
        if handle in self.media_map:
            return self.media_map[handle]
        return None

    def get_raw_tag_data(self, handle):
        if handle in self.tag_map:
            return self.tag_map[handle]
        return None

    def get_raw_event_data(self, handle):
        if handle in self.event_map:
            return self.event_map[handle]
        return None

    def add_person(self, person, trans, set_gid=True):
        if not person.handle:
            person.handle = create_id()
        if not person.gramps_id and set_gid:
            person.gramps_id = self.find_next_person_gramps_id()
        self.commit_person(person, trans)
        return person.handle

    def add_family(self, family, trans, set_gid=True):
        if not family.handle:
            family.handle = create_id()
        if not family.gramps_id and set_gid:
            family.gramps_id = self.find_next_family_gramps_id()
        self.commit_family(family, trans)
        return family.handle

    def add_citation(self, citation, trans, set_gid=True):
        if not citation.handle:
            citation.handle = create_id()
        if not citation.gramps_id and set_gid:
            citation.gramps_id = self.find_next_citation_gramps_id()
        self.commit_citation(citation, trans)
        return citation.handle

    def add_source(self, source, trans, set_gid=True):
        if not source.handle:
            source.handle = create_id()
        if not source.gramps_id and set_gid:
            source.gramps_id = self.find_next_source_gramps_id()
        self.commit_source(source, trans)
        return source.handle

    def add_repository(self, repository, trans, set_gid=True):
        if not repository.handle:
            repository.handle = create_id()
        if not repository.gramps_id and set_gid:
            repository.gramps_id = self.find_next_repository_gramps_id()
        self.commit_repository(repository, trans)
        return repository.handle

    def add_note(self, note, trans, set_gid=True):
        if not note.handle:
            note.handle = create_id()
        if not note.gramps_id and set_gid:
            note.gramps_id = self.find_next_note_gramps_id()
        self.commit_note(note, trans)
        return note.handle

    def add_place(self, place, trans, set_gid=True):
        if not place.handle:
            place.handle = create_id()
        if not place.gramps_id and set_gid:
            place.gramps_id = self.find_next_place_gramps_id()
        self.commit_place(place, trans)
        return place.handle

    def add_event(self, event, trans, set_gid=True):
        if not event.handle:
            event.handle = create_id()
        if not event.gramps_id and set_gid:
            event.gramps_id = self.find_next_event_gramps_id()
        self.commit_event(event, trans)
        return event.handle

    def add_tag(self, tag, trans):
        if not tag.handle:
            tag.handle = create_id()
        self.commit_tag(tag, trans)
        return tag.handle

    def add_object(self, obj, transaction, set_gid=True):
        """
        Add a MediaObject to the database, assigning internal IDs if they have
        not already been defined.
        
        If not set_gid, then gramps_id is not set.
        """
        if not obj.handle:
            obj.handle = create_id()
        if not obj.gramps_id and set_gid:
            obj.gramps_id = self.find_next_object_gramps_id()
        self.commit_media_object(obj, transaction)
        return obj.handle

    def commit_person(self, person, trans, change_time=None):
        emit = None
        old_person = None
        if person.handle in self.person_map:
            emit = "person-update"
            old_person = self.get_person_from_handle(person.handle)
            # Update gender statistics if necessary
            if (old_person.gender != person.gender or
                old_person.primary_name.first_name !=
                  person.primary_name.first_name):

                self.genderStats.uncount_person(old_person)
                self.genderStats.count_person(person)
            # Update surname list if necessary
            if (self._order_by_person_key(person) != 
                self._order_by_person_key(old_person)):
                self.remove_from_surname_list(old_person)
                self.add_to_surname_list(person, trans.batch)
            given_name, surname, gender_type = self.get_person_data(person)
            # update the person:
            self.dbapi.execute("""UPDATE person SET gramps_id = ?, 
                                                    order_by = ?,
                                                    blob_data = ?,
                                                    given_name = ?,
                                                    surname = ?,
                                                    gender_type = ?
                                                WHERE handle = ?;""",
                               [person.gramps_id, 
                                self._order_by_person_key(person),
                                pickle.dumps(person.serialize()),
                                given_name,
                                surname,
                                gender_type,
                                person.handle])
        else:
            emit = "person-add"
            self.genderStats.count_person(person)
            self.add_to_surname_list(person, trans.batch)
            given_name, surname, gender_type = self.get_person_data(person)
            # Insert the person:
            self.dbapi.execute("""INSERT INTO person (handle, order_by, gramps_id, blob_data,
                                                      given_name, surname, gender_type)
                            VALUES(?, ?, ?, ?, ?, ?, ?);""", 
                               [person.handle, 
                                self._order_by_person_key(person),
                                person.gramps_id, 
                                pickle.dumps(person.serialize()),
                                given_name, surname, gender_type])
        if not trans.batch:
            self.update_backlinks(person)
            self.dbapi.commit()
            if old_person:
                trans.add(PERSON_KEY, TXNUPD, person.handle, 
                          old_person.serialize(), 
                          person.serialize())
            else:
                trans.add(PERSON_KEY, TXNADD, person.handle, 
                          None, 
                          person.serialize())
        # Other misc update tasks:
        self.individual_attributes.update(
            [str(attr.type) for attr in person.attribute_list
             if attr.type.is_custom() and str(attr.type)])

        self.event_role_names.update([str(eref.role)
                                      for eref in person.event_ref_list
                                      if eref.role.is_custom()])

        self.name_types.update([str(name.type)
                                for name in ([person.primary_name]
                                             + person.alternate_names)
                                if name.type.is_custom()])
        all_surn = []  # new list we will use for storage
        all_surn += person.primary_name.get_surname_list() 
        for asurname in person.alternate_names:
            all_surn += asurname.get_surname_list()
        self.origin_types.update([str(surn.origintype) for surn in all_surn
                                if surn.origintype.is_custom()])
        all_surn = None
        self.url_types.update([str(url.type) for url in person.urls
                               if url.type.is_custom()])
        attr_list = []
        for mref in person.media_list:
            attr_list += [str(attr.type) for attr in mref.attribute_list
                          if attr.type.is_custom() and str(attr.type)]
        self.media_attributes.update(attr_list)
        # Emit after added:
        if emit:
            self.emit(emit, ([person.handle],))
        self.has_changed = True

    def add_to_surname_list(self, person, batch_transaction):
        """
        Add surname to surname list
        """
        if batch_transaction:
            return
        name = None
        primary_name = person.get_primary_name()
        if primary_name:
            surname_list = primary_name.get_surname_list()
            if len(surname_list) > 0:
                name = surname_list[0].surname
        if name is None:
            return
        i = bisect.bisect(self.surname_list, name)
        if 0 < i <= len(self.surname_list):
            if self.surname_list[i-1] != name:
                self.surname_list.insert(i, name)
        else:
            self.surname_list.insert(i, name)

    def remove_from_surname_list(self, person):
        """
        Check whether there are persons with the same surname left in
        the database. 
        
        If not then we need to remove the name from the list.
        The function must be overridden in the derived class.
        """
        name = None
        primary_name = person.get_primary_name()
        if primary_name:
            surname_list = primary_name.get_surname_list()
            if len(surname_list) > 0:
                name = surname_list[0].surname
        if name is None:
            return
        if name in self.surname_list:
            self.surname_list.remove(name)

    def commit_family(self, family, trans, change_time=None):
        emit = None
        old_family = None
        if family.handle in self.family_map:
            emit = "family-update"
            old_family = self.get_family_from_handle(family.handle).serialize()
            self.dbapi.execute("""UPDATE family SET gramps_id = ?, 
                                                    blob_data = ? 
                                                WHERE handle = ?;""",
                               [family.gramps_id, 
                                pickle.dumps(family.serialize()),
                                family.handle])
        else:
            emit = "family-add"
            self.dbapi.execute("""INSERT INTO family (handle, gramps_id, blob_data)
                    VALUES(?, ?, ?);""", 
                               [family.handle, family.gramps_id, 
                                pickle.dumps(family.serialize())])
        if not trans.batch:
            self.update_backlinks(family)
            self.dbapi.commit()
            op = TXNUPD if old_family else TXNADD
            trans.add(FAMILY_KEY, op, family.handle, 
                      old_family, 
                      family.serialize())

        # Misc updates:
        self.family_attributes.update(
            [str(attr.type) for attr in family.attribute_list
             if attr.type.is_custom() and str(attr.type)])

        rel_list = []
        for ref in family.child_ref_list:
            if ref.frel.is_custom():
                rel_list.append(str(ref.frel))
            if ref.mrel.is_custom():
                rel_list.append(str(ref.mrel))
        self.child_ref_types.update(rel_list)

        self.event_role_names.update(
            [str(eref.role) for eref in family.event_ref_list
             if eref.role.is_custom()])

        if family.type.is_custom():
            self.family_rel_types.add(str(family.type))

        attr_list = []
        for mref in family.media_list:
            attr_list += [str(attr.type) for attr in mref.attribute_list
                          if attr.type.is_custom() and str(attr.type)]
        self.media_attributes.update(attr_list)
        # Emit after added:
        if emit:
            self.emit(emit, ([family.handle],))
        self.has_changed = True

    def commit_citation(self, citation, trans, change_time=None):
        emit = None
        old_citation = None
        if citation.handle in self.citation_map:
            emit = "citation-update"
            old_citation = self.get_citation_from_handle(citation.handle).serialize()
            self.dbapi.execute("""UPDATE citation SET gramps_id = ?, 
                                                      order_by = ?,
                                                      blob_data = ? 
                                                WHERE handle = ?;""",
                               [citation.gramps_id, 
                                self._order_by_citation_key(citation),
                                pickle.dumps(citation.serialize()),
                                citation.handle])
        else:
            emit = "citation-add"
            self.dbapi.execute("""INSERT INTO citation (handle, order_by, gramps_id, blob_data)
                     VALUES(?, ?, ?, ?);""", 
                       [citation.handle, 
                        self._order_by_citation_key(citation),
                        citation.gramps_id, 
                        pickle.dumps(citation.serialize())])
        if not trans.batch:
            self.update_backlinks(citation)
            self.dbapi.commit()
            op = TXNUPD if old_citation else TXNADD
            trans.add(CITATION_KEY, op, citation.handle, 
                      old_citation, 
                      citation.serialize())
        # Misc updates:
        attr_list = []
        for mref in citation.media_list:
            attr_list += [str(attr.type) for attr in mref.attribute_list
                          if attr.type.is_custom() and str(attr.type)]
        self.media_attributes.update(attr_list)

        self.source_attributes.update(
            [str(attr.type) for attr in citation.attribute_list
             if attr.type.is_custom() and str(attr.type)])

        # Emit after added:
        if emit:
            self.emit(emit, ([citation.handle],))
        self.has_changed = True

    def commit_source(self, source, trans, change_time=None):
        emit = None
        old_source = None
        if source.handle in self.source_map:
            emit = "source-update"
            old_source = self.get_source_from_handle(source.handle).serialize()
            self.dbapi.execute("""UPDATE source SET gramps_id = ?, 
                                                    order_by = ?,
                                                    blob_data = ? 
                                                WHERE handle = ?;""",
                               [source.gramps_id, 
                                self._order_by_source_key(source),
                                pickle.dumps(source.serialize()),
                                source.handle])
        else:
            emit = "source-add"
            self.dbapi.execute("""INSERT INTO source (handle, order_by, gramps_id, blob_data)
                    VALUES(?, ?, ?, ?);""", 
                       [source.handle, 
                        self._order_by_source_key(source),
                        source.gramps_id, 
                        pickle.dumps(source.serialize())])
        if not trans.batch:
            self.update_backlinks(source)
            self.dbapi.commit()
            op = TXNUPD if old_source else TXNADD
            trans.add(SOURCE_KEY, op, source.handle, 
                      old_source, 
                      source.serialize())
        # Misc updates:
        self.source_media_types.update(
            [str(ref.media_type) for ref in source.reporef_list
             if ref.media_type.is_custom()])       

        attr_list = []
        for mref in source.media_list:
            attr_list += [str(attr.type) for attr in mref.attribute_list
                          if attr.type.is_custom() and str(attr.type)]
        self.media_attributes.update(attr_list)
        self.source_attributes.update(
            [str(attr.type) for attr in source.attribute_list
             if attr.type.is_custom() and str(attr.type)])
        # Emit after added:
        if emit:
            self.emit(emit, ([source.handle],))
        self.has_changed = True

    def commit_repository(self, repository, trans, change_time=None):
        emit = None
        old_repository = None
        if repository.handle in self.repository_map:
            emit = "repository-update"
            old_repository = self.get_repository_from_handle(repository.handle).serialize()
            self.dbapi.execute("""UPDATE repository SET gramps_id = ?, 
                                                    blob_data = ? 
                                                WHERE handle = ?;""",
                               [repository.gramps_id, 
                                pickle.dumps(repository.serialize()),
                                repository.handle])
        else:
            emit = "repository-add"
            self.dbapi.execute("""INSERT INTO repository (handle, gramps_id, blob_data)
                     VALUES(?, ?, ?);""", 
                       [repository.handle, repository.gramps_id, pickle.dumps(repository.serialize())])
        if not trans.batch:
            self.update_backlinks(repository)
            self.dbapi.commit()
            op = TXNUPD if old_repository else TXNADD
            trans.add(REPOSITORY_KEY, op, repository.handle, 
                      old_repository, 
                      repository.serialize())
        # Misc updates:
        if repository.type.is_custom():
            self.repository_types.add(str(repository.type))

        self.url_types.update([str(url.type) for url in repository.urls
                               if url.type.is_custom()])
        # Emit after added:
        if emit:
            self.emit(emit, ([repository.handle],))
        self.has_changed = True

    def commit_note(self, note, trans, change_time=None):
        emit = None
        old_note = None
        if note.handle in self.note_map:
            emit = "note-update"
            old_note = self.get_note_from_handle(note.handle).serialize()
            self.dbapi.execute("""UPDATE note SET gramps_id = ?, 
                                                    blob_data = ? 
                                                WHERE handle = ?;""",
                               [note.gramps_id, 
                                pickle.dumps(note.serialize()),
                                note.handle])
        else:
            emit = "note-add"
            self.dbapi.execute("""INSERT INTO note (handle, gramps_id, blob_data)
                     VALUES(?, ?, ?);""", 
                       [note.handle, note.gramps_id, pickle.dumps(note.serialize())])
        if not trans.batch:
            self.update_backlinks(note)
            self.dbapi.commit()
            op = TXNUPD if old_note else TXNADD
            trans.add(NOTE_KEY, op, note.handle, 
                      old_note, 
                      note.serialize())
        # Misc updates:
        if note.type.is_custom():
            self.note_types.add(str(note.type))        
        # Emit after added:
        if emit:
            self.emit(emit, ([note.handle],))
        self.has_changed = True

    def commit_place(self, place, trans, change_time=None):
        emit = None
        old_place = None
        if place.handle in self.place_map:
            emit = "place-update"
            old_place = self.get_place_from_handle(place.handle).serialize()
            self.dbapi.execute("""UPDATE place SET gramps_id = ?, 
                                                   order_by = ?,
                                                   blob_data = ? 
                                                WHERE handle = ?;""",
                               [place.gramps_id, 
                                self._order_by_place_key(place),
                                pickle.dumps(place.serialize()),
                                place.handle])
        else:
            emit = "place-add"
            self.dbapi.execute("""INSERT INTO place (handle, order_by, gramps_id, blob_data)
                    VALUES(?, ?, ?, ?);""", 
                       [place.handle, 
                        self._order_by_place_key(place),
                        place.gramps_id, 
                        pickle.dumps(place.serialize())])
        if not trans.batch:
            self.update_backlinks(place)
            self.dbapi.commit()
            op = TXNUPD if old_place else TXNADD
            trans.add(PLACE_KEY, op, place.handle, 
                      old_place, 
                      place.serialize())
        # Misc updates:
        if place.get_type().is_custom():
            self.place_types.add(str(place.get_type()))

        self.url_types.update([str(url.type) for url in place.urls
                               if url.type.is_custom()])

        attr_list = []
        for mref in place.media_list:
            attr_list += [str(attr.type) for attr in mref.attribute_list
                          if attr.type.is_custom() and str(attr.type)]
        self.media_attributes.update(attr_list)
        # Emit after added:
        if emit:
            self.emit(emit, ([place.handle],))
        self.has_changed = True

    def commit_event(self, event, trans, change_time=None):
        emit = None
        old_event = None
        if event.handle in self.event_map:
            emit = "event-update"
            old_event = self.get_event_from_handle(event.handle).serialize()
            self.dbapi.execute("""UPDATE event SET gramps_id = ?, 
                                                    blob_data = ? 
                                                WHERE handle = ?;""",
                               [event.gramps_id, 
                                pickle.dumps(event.serialize()),
                                event.handle])
        else:
            emit = "event-add"
            self.dbapi.execute("""INSERT INTO event (handle, gramps_id, blob_data)
                  VALUES(?, ?, ?);""", 
                       [event.handle, 
                        event.gramps_id, 
                        pickle.dumps(event.serialize())])
        if not trans.batch:
            self.update_backlinks(event)
            self.dbapi.commit()
            op = TXNUPD if old_event else TXNADD
            trans.add(EVENT_KEY, op, event.handle, 
                      old_event, 
                      event.serialize())
        # Misc updates:
        self.event_attributes.update(
            [str(attr.type) for attr in event.attribute_list
             if attr.type.is_custom() and str(attr.type)])
        if event.type.is_custom():
            self.event_names.add(str(event.type))
        attr_list = []
        for mref in event.media_list:
            attr_list += [str(attr.type) for attr in mref.attribute_list
                          if attr.type.is_custom() and str(attr.type)]
        self.media_attributes.update(attr_list)
        # Emit after added:
        if emit:
            self.emit(emit, ([event.handle],))
        self.has_changed = True

    def update_backlinks(self, obj):
        # First, delete the current references:
        self.dbapi.execute("DELETE FROM reference WHERE obj_handle = ?;",
                           [obj.handle])
        # Now, add the current ones:
        references = set(obj.get_referenced_handles_recursively())
        for (ref_class_name, ref_handle) in references:
            self.dbapi.execute("""INSERT INTO reference 
                       (obj_handle, obj_class, ref_handle, ref_class)
                       VALUES(?, ?, ?, ?);""",
                               [obj.handle, 
                                obj.__class__.__name__,
                                ref_handle, 
                                ref_class_name])
        # This function is followed by a commit.

    def commit_tag(self, tag, trans, change_time=None):
        emit = None
        if tag.handle in self.tag_map:
            emit = "tag-update"
            self.dbapi.execute("""UPDATE tag SET blob_data = ?,
                                                 order_by = ?
                                         WHERE handle = ?;""",
                               [pickle.dumps(tag.serialize()),
                                self._order_by_tag_key(tag),
                                tag.handle])
        else:
            emit = "tag-add"
            self.dbapi.execute("""INSERT INTO tag (handle, order_by, blob_data)
                  VALUES(?, ?, ?);""", 
                       [tag.handle, 
                        self._order_by_tag_key(tag),
                        pickle.dumps(tag.serialize())])
        if not trans.batch:
            self.update_backlinks(tag)
            self.dbapi.commit()
        # Emit after added:
        if emit:
            self.emit(emit, ([tag.handle],))

    def commit_media_object(self, media, trans, change_time=None):
        emit = None
        old_media = None
        if media.handle in self.media_map:
            emit = "media-update"
            old_media = self.get_object_from_handle(media.handle).serialize()
            self.dbapi.execute("""UPDATE media SET gramps_id = ?, 
                                                   order_by = ?,
                                                   blob_data = ? 
                                                WHERE handle = ?;""",
                               [media.gramps_id, 
                                self._order_by_media_key(media),
                                pickle.dumps(media.serialize()),
                                media.handle])
        else:
            emit = "media-add"
            self.dbapi.execute("""INSERT INTO media (handle, order_by, gramps_id, blob_data)
                  VALUES(?, ?, ?, ?);""", 
                       [media.handle, 
                        self._order_by_media_key(media),
                        media.gramps_id, 
                        pickle.dumps(media.serialize())])
        if not trans.batch:
            self.update_backlinks(media)
            self.dbapi.commit()
            op = TXNUPD if old_media else TXNADD
            trans.add(MEDIA_KEY, op, media.handle, 
                      old_media, 
                      media.serialize())
        # Misc updates:
        self.media_attributes.update(
            [str(attr.type) for attr in media.attribute_list
             if attr.type.is_custom() and str(attr.type)])
        # Emit after added:
        if emit:
            self.emit(emit, ([media.handle],))

    def get_gramps_ids(self, obj_key):
        key2table = {
            PERSON_KEY:     self.person_id_map, 
            FAMILY_KEY:     self.family_id_map, 
            CITATION_KEY:   self.citation_id_map, 
            SOURCE_KEY:     self.source_id_map, 
            EVENT_KEY:      self.event_id_map, 
            MEDIA_KEY:      self.media_id_map, 
            PLACE_KEY:      self.place_id_map, 
            REPOSITORY_KEY: self.repository_id_map, 
            NOTE_KEY:       self.note_id_map, 
            }
        return list(key2table[obj_key].keys())

    def set_researcher(self, owner):
        self.owner.set_from(owner)

    def get_researcher(self):
        return self.owner

    def request_rebuild(self):
        self.emit('person-rebuild')
        self.emit('family-rebuild')
        self.emit('place-rebuild')
        self.emit('source-rebuild')
        self.emit('citation-rebuild')
        self.emit('media-rebuild')
        self.emit('event-rebuild')
        self.emit('repository-rebuild')
        self.emit('note-rebuild')
        self.emit('tag-rebuild')

    def copy_from_db(self, db):
        """
        A (possibily) implementation-specific method to get data from
        db into this database.
        """
        for key in db._tables.keys():
            cursor = db._tables[key]["cursor_func"]
            class_ = db._tables[key]["class_func"]
            for (handle, data) in cursor():
                map = getattr(self, "%s_map" % key.lower())
                map[handle] = class_.create(data)

    def get_transaction_class(self):
        """
        Get the transaction class associated with this database backend.
        """
        return DBAPITxn

    def get_from_name_and_handle(self, table_name, handle):
        """
        Returns a gen.lib object (or None) given table_name and
        handle.

        Examples:

        >>> self.get_from_name_and_handle("Person", "a7ad62365bc652387008")
        >>> self.get_from_name_and_handle("Media", "c3434653675bcd736f23")
        """
        if table_name in self._tables:
            return self._tables[table_name]["handle_func"](handle)
        return None

    def get_from_name_and_gramps_id(self, table_name, gramps_id):
        """
        Returns a gen.lib object (or None) given table_name and
        Gramps ID.

        Examples:

        >>> self.get_from_name_and_gramps_id("Person", "I00002")
        >>> self.get_from_name_and_gramps_id("Family", "F056")
        >>> self.get_from_name_and_gramps_id("Media", "M00012")
        """
        if table_name in self._tables:
            return self._tables[table_name]["gramps_id_func"](gramps_id)
        return None

    def remove_person(self, handle, transaction):
        """
        Remove the Person specified by the database handle from the database, 
        preserving the change in the passed transaction. 
        """

        if self.readonly or not handle:
            return
        if handle in self.person_map:
            person = Person.create(self.person_map[handle])
            self.dbapi.execute("DELETE FROM person WHERE handle = ?;", [handle])
            self.emit("person-delete", ([handle],))
            if not transaction.batch:
                self.dbapi.commit()
                transaction.add(PERSON_KEY, TXNDEL, person.handle, 
                                person.serialize(), None)

    def remove_source(self, handle, transaction):
        """
        Remove the Source specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.source_map, 
                         self.source_id_map, SOURCE_KEY)

    def remove_citation(self, handle, transaction):
        """
        Remove the Citation specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.citation_map, 
                         self.citation_id_map, CITATION_KEY)

    def remove_event(self, handle, transaction):
        """
        Remove the Event specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.event_map, 
                         self.event_id_map, EVENT_KEY)

    def remove_object(self, handle, transaction):
        """
        Remove the MediaObjectPerson specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.media_map, 
                         self.media_id_map, MEDIA_KEY)

    def remove_place(self, handle, transaction):
        """
        Remove the Place specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.place_map, 
                         self.place_id_map, PLACE_KEY)

    def remove_family(self, handle, transaction):
        """
        Remove the Family specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.family_map, 
                         self.family_id_map, FAMILY_KEY)

    def remove_repository(self, handle, transaction):
        """
        Remove the Repository specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.repository_map, 
                         self.repository_id_map, REPOSITORY_KEY)

    def remove_note(self, handle, transaction):
        """
        Remove the Note specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.note_map, 
                         self.note_id_map, NOTE_KEY)

    def remove_tag(self, handle, transaction):
        """
        Remove the Tag specified by the database handle from the
        database, preserving the change in the passed transaction. 
        """
        self.__do_remove(handle, transaction, self.tag_map, 
                         None, TAG_KEY)

    def is_empty(self):
        """
        Return true if there are no [primary] records in the database
        """
        for table in self._tables:
            if len(self._tables[table]["handles_func"]()) > 0:
                return False
        return True

    def __do_remove(self, handle, transaction, data_map, data_id_map, key):
        key2table = {
            PERSON_KEY:     "person", 
            FAMILY_KEY:     "family", 
            SOURCE_KEY:     "source", 
            CITATION_KEY:   "citation", 
            EVENT_KEY:      "event", 
            MEDIA_KEY:      "media", 
            PLACE_KEY:      "place", 
            REPOSITORY_KEY: "repository", 
            NOTE_KEY:       "note", 
            }
        if self.readonly or not handle:
            return
        if handle in data_map:
            self.dbapi.execute("DELETE FROM %s WHERE handle = ?;" % key2table[key], 
                               [handle])
            self.emit(KEY_TO_NAME_MAP[key] + "-delete", ([handle],))
            if not transaction.batch:
                self.dbapi.commit()
                data = data_map[handle]
                transaction.add(key, TXNDEL, handle, data, None)

    def close(self):
        if self._directory:
            filename = os.path.join(self._directory, "meta_data.db")
            touch(filename)
            # Save metadata
            self.set_metadata('bookmarks', self.bookmarks.get())
            self.set_metadata('family_bookmarks', self.family_bookmarks.get())
            self.set_metadata('event_bookmarks', self.event_bookmarks.get())
            self.set_metadata('source_bookmarks', self.source_bookmarks.get())
            self.set_metadata('citation_bookmarks', self.citation_bookmarks.get())
            self.set_metadata('repo_bookmarks', self.repo_bookmarks.get())
            self.set_metadata('media_bookmarks', self.media_bookmarks.get())
            self.set_metadata('place_bookmarks', self.place_bookmarks.get())
            self.set_metadata('note_bookmarks', self.note_bookmarks.get())
            
            # Custom type values, sets
            self.set_metadata('event_names', self.event_names)
            self.set_metadata('fattr_names', self.family_attributes)
            self.set_metadata('pattr_names', self.individual_attributes)
            self.set_metadata('sattr_names', self.source_attributes)
            self.set_metadata('marker_names', self.marker_names)
            self.set_metadata('child_refs', self.child_ref_types)
            self.set_metadata('family_rels', self.family_rel_types)
            self.set_metadata('event_roles', self.event_role_names)
            self.set_metadata('name_types', self.name_types)
            self.set_metadata('origin_types', self.origin_types)
            self.set_metadata('repo_types', self.repository_types)
            self.set_metadata('note_types', self.note_types)
            self.set_metadata('sm_types', self.source_media_types)
            self.set_metadata('url_types', self.url_types)
            self.set_metadata('mattr_names', self.media_attributes)
            self.set_metadata('eattr_names', self.event_attributes)
            self.set_metadata('place_types', self.place_types)
            
            # Save misc items:
            self.save_surname_list()
            self.save_gender_stats(self.genderStats)
            
            self.dbapi.close()

    def find_backlink_handles(self, handle, include_classes=None):
        """
        Find all objects that hold a reference to the object handle.
        
        Returns an interator over a list of (class_name, handle) tuples.

        :param handle: handle of the object to search for.
        :type handle: database handle
        :param include_classes: list of class names to include in the results.
            Default: None means include all classes.
        :type include_classes: list of class names

        Note that this is a generator function, it returns a iterator for
        use in loops. If you want a list of the results use::

            result_list = list(find_backlink_handles(handle))
        """
        self.dbapi.execute("SELECT obj_class, obj_handle FROM reference WHERE ref_handle = ?;",
                                 [handle])
        rows = self.dbapi.fetchall()
        for row in rows:
            if (include_classes is None) or (row[0] in include_classes):
                yield (row[0], row[1])

    def find_initial_person(self):
        handle = self.get_default_handle()
        person = None
        if handle:
            person = self.get_person_from_handle(handle)
            if person:
                return person
        self.dbapi.execute("SELECT handle FROM person;")
        row = self.dbapi.fetchone()
        if row:
            return self.get_person_from_handle(row[0])

    def get_bookmarks(self):
        return self.bookmarks

    def get_citation_bookmarks(self):
        return self.citation_bookmarks

    def get_default_handle(self):
        return self.get_metadata("default-person-handle", None)

    def get_surname_list(self):
        """
        Return the list of locale-sorted surnames contained in the database.
        """
        return self.surname_list

    def get_event_attribute_types(self):
        """
        Return a list of all Attribute types assocated with Event instances
        in the database.
        """
        return list(self.event_attributes)

    def get_event_types(self):
        """
        Return a list of all event types in the database.
        """
        return list(self.event_names)

    def get_person_event_types(self):
        """
        Deprecated:  Use get_event_types
        """
        return list(self.event_names)

    def get_person_attribute_types(self):
        """
        Return a list of all Attribute types assocated with Person instances 
        in the database.
        """
        return list(self.individual_attributes)

    def get_family_attribute_types(self):
        """
        Return a list of all Attribute types assocated with Family instances 
        in the database.
        """
        return list(self.family_attributes)

    def get_family_event_types(self):
        """
        Deprecated:  Use get_event_types
        """
        return list(self.event_names)

    def get_media_attribute_types(self):
        """
        Return a list of all Attribute types assocated with Media and MediaRef 
        instances in the database.
        """
        return list(self.media_attributes)

    def get_family_relation_types(self):
        """
        Return a list of all relationship types assocated with Family
        instances in the database.
        """
        return list(self.family_rel_types)

    def get_child_reference_types(self):
        """
        Return a list of all child reference types assocated with Family
        instances in the database.
        """
        return list(self.child_ref_types)

    def get_event_roles(self):
        """
        Return a list of all custom event role names assocated with Event
        instances in the database.
        """
        return list(self.event_role_names)

    def get_name_types(self):
        """
        Return a list of all custom names types assocated with Person
        instances in the database.
        """
        return list(self.name_types)

    def get_origin_types(self):
        """
        Return a list of all custom origin types assocated with Person/Surname
        instances in the database.
        """
        return list(self.origin_types)

    def get_repository_types(self):
        """
        Return a list of all custom repository types assocated with Repository 
        instances in the database.
        """
        return list(self.repository_types)

    def get_note_types(self):
        """
        Return a list of all custom note types assocated with Note instances 
        in the database.
        """
        return list(self.note_types)

    def get_source_attribute_types(self):
        """
        Return a list of all Attribute types assocated with Source/Citation
        instances in the database.
        """
        return list(self.source_attributes)

    def get_source_media_types(self):
        """
        Return a list of all custom source media types assocated with Source 
        instances in the database.
        """
        return list(self.source_media_types)

    def get_url_types(self):
        """
        Return a list of all custom names types assocated with Url instances 
        in the database.
        """
        return list(self.url_types)

    def get_place_types(self):
        """
        Return a list of all custom place types assocated with Place instances
        in the database.
        """
        return list(self.place_types)

    def get_event_bookmarks(self):
        return self.event_bookmarks

    def get_family_bookmarks(self):
        return self.family_bookmarks

    def get_media_bookmarks(self):
        return self.media_bookmarks

    def get_note_bookmarks(self):
        return self.note_bookmarks

    def get_place_bookmarks(self):
        return self.place_bookmarks

    def get_repo_bookmarks(self):
        return self.repo_bookmarks

    def get_save_path(self):
        return self._directory

    def get_source_bookmarks(self):
        return self.source_bookmarks

    def is_open(self):
        return self._directory is not None

    def iter_citation_handles(self):
        self.dbapi.execute("SELECT handle FROM citation;")
        row = self.dbapi.fetchone()
        while row:
            yield row[0]
            row = self.dbapi.fetchone()

    def iter_citations(self):
        return (Citation.create(data[1]) for data in self.get_citation_cursor())

    def iter_event_handles(self):
        self.dbapi.execute("SELECT handle FROM event;")
        row = self.dbapi.fetchone()
        while row:
            yield row[0]
            row = self.dbapi.fetchone()

    def iter_events(self):
        return (Event.create(data[1]) for data in self.get_event_cursor())

    def iter_media_objects(self):
        return (MediaObject.create(data[1]) for data in self.get_media_cursor())

    def iter_media_object_handles(self):
        self.dbapi.execute("SELECT handle FROM media;")
        row = self.dbapi.fetchone()
        while row:
            yield row[0]
            row = self.dbapi.fetchone()

    def iter_note_handles(self):
        self.dbapi.execute("SELECT handle FROM note;")
        row = self.dbapi.fetchone()
        while row:
            yield row[0]
            row = self.dbapi.fetchone()

    def iter_notes(self):
        return (Note.create(data[1]) for data in self.get_note_cursor())

    def iter_place_handles(self):
        self.dbapi.execute("SELECT handle FROM place;")
        row = self.dbapi.fetchone()
        while row:
            yield row[0]
            row = self.dbapi.fetchone()

    def iter_places(self):
        return (Place.create(data[1]) for data in self.get_place_cursor())

    def iter_repositories(self):
        return (Repository.create(data[1]) for data in self.get_repository_cursor())

    def iter_repository_handles(self):
        self.dbapi.execute("SELECT handle FROM repository;")
        row = self.dbapi.fetchone()
        while row:
            yield row[0]
            row = self.dbapi.fetchone()

    def iter_source_handles(self):
        self.dbapi.execute("SELECT handle FROM source;")
        row = self.dbapi.fetchone()
        while row:
            yield row[0]
            row = self.dbapi.fetchone()

    def iter_sources(self):
        return (Source.create(data[1]) for data in self.get_source_cursor())

    def iter_tag_handles(self):
        self.dbapi.execute("SELECT handle FROM tag;")
        row = self.dbapi.fetchone()
        while row:
            yield row[0]
            row = self.dbapi.fetchone()

    def iter_tags(self):
        return (Tag.create(data[1]) for data in self.get_tag_cursor())

    def load(self, directory, callback=None, mode=None, 
             force_schema_upgrade=False, 
             force_bsddb_upgrade=False, 
             force_bsddb_downgrade=False, 
             force_python_upgrade=False):
        # Run code from directory
        default_settings = {"__file__": 
                            os.path.join(directory, "default_settings.py"),
                            "dbapi_support": dbapi_support}
        settings_file = os.path.join(directory, "default_settings.py")
        with open(settings_file) as f:
            code = compile(f.read(), settings_file, 'exec')
            exec(code, globals(), default_settings)

        self.dbapi = default_settings["dbapi"]
            
        # make sure schema is up to date:
        self.dbapi.try_execute("""CREATE TABLE person (
                                    handle    VARCHAR(50) PRIMARY KEY NOT NULL,
                                    given_name     TEXT        ,
                                    surname        TEXT        ,
                                    gender_type    INTEGER     ,
                                    order_by  TEXT             ,
                                    gramps_id TEXT             ,
                                    blob_data      BLOB
        );""")
        self.dbapi.try_execute("""CREATE TABLE family (
                                    handle    VARCHAR(50) PRIMARY KEY NOT NULL,
                                    gramps_id TEXT             ,
                                    blob_data      BLOB
        );""")
        self.dbapi.try_execute("""CREATE TABLE source (
                                    handle    VARCHAR(50) PRIMARY KEY NOT NULL,
                                    order_by  TEXT             ,
                                    gramps_id TEXT             ,
                                    blob_data      BLOB
        );""")
        self.dbapi.try_execute("""CREATE TABLE citation (
                                    handle    VARCHAR(50) PRIMARY KEY NOT NULL,
                                    order_by  TEXT             ,
                                    gramps_id TEXT             ,
                                    blob_data      BLOB
        );""")
        self.dbapi.try_execute("""CREATE TABLE event (
                                    handle    VARCHAR(50) PRIMARY KEY NOT NULL,
                                    gramps_id TEXT             ,
                                    blob_data      BLOB
        );""")
        self.dbapi.try_execute("""CREATE TABLE media (
                                    handle    VARCHAR(50) PRIMARY KEY NOT NULL,
                                    order_by  TEXT             ,
                                    gramps_id TEXT             ,
                                    blob_data      BLOB
        );""")
        self.dbapi.try_execute("""CREATE TABLE place (
                                    handle    VARCHAR(50) PRIMARY KEY NOT NULL,
                                    order_by  TEXT             ,
                                    gramps_id TEXT             ,
                                    blob_data      BLOB
        );""")
        self.dbapi.try_execute("""CREATE TABLE repository (
                                    handle    VARCHAR(50) PRIMARY KEY NOT NULL,
                                    gramps_id TEXT             ,
                                    blob_data      BLOB
        );""")
        self.dbapi.try_execute("""CREATE TABLE note (
                                    handle    VARCHAR(50) PRIMARY KEY NOT NULL,
                                    gramps_id TEXT             ,
                                    blob_data      BLOB
        );""")
        self.dbapi.try_execute("""CREATE TABLE tag (
                                    handle    VARCHAR(50) PRIMARY KEY NOT NULL,
                                    order_by  TEXT             ,
                                    blob_data      BLOB
        );""")
        # Secondary:
        self.dbapi.try_execute("""CREATE TABLE reference (
                                    obj_handle    VARCHAR(50),
                                    obj_class     TEXT,
                                    ref_handle    VARCHAR(50),
                                    ref_class     TEXT
        );""")
        self.dbapi.try_execute("""CREATE TABLE name_group (
                                    name     VARCHAR(50) PRIMARY KEY NOT NULL,
                                    grouping TEXT
        );""")
        self.dbapi.try_execute("""CREATE TABLE metadata (
                                    setting  VARCHAR(50) PRIMARY KEY NOT NULL,
                                    value    BLOB
        );""")
        self.dbapi.try_execute("""CREATE TABLE gender_stats (
                                    given_name TEXT, 
                                    female     INTEGER, 
                                    male       INTEGER, 
                                    unknown    INTEGER
        );""") 
        ## Indices:
        self.dbapi.try_execute("""CREATE INDEX  
                                  person_order_by ON person (order_by(50));
        """)
        self.dbapi.try_execute("""CREATE INDEX  
                                  person_gramps_id ON person (gramps_id(50));
        """)
        self.dbapi.try_execute("""CREATE INDEX  
                                  person_surname ON person (surname(50));
        """)
        self.dbapi.try_execute("""CREATE INDEX  
                                  person_given_name ON person (given_name(50));
        """)
        self.dbapi.try_execute("""CREATE INDEX  
                                  source_order_by ON source (order_by(50));
        """)
        self.dbapi.try_execute("""CREATE INDEX  
                                  source_gramps_id ON source (gramps_id(50));
        """)
        self.dbapi.try_execute("""CREATE INDEX  
                                  citation_order_by ON citation (order_by(50));
        """)
        self.dbapi.try_execute("""CREATE INDEX  
                                  citation_gramps_id ON citation (gramps_id(50));
        """)
        self.dbapi.try_execute("""CREATE INDEX  
                                  media_order_by ON media (order_by(50));
        """)
        self.dbapi.try_execute("""CREATE INDEX  
                                  media_gramps_id ON media (gramps_id(50));
        """)
        self.dbapi.try_execute("""CREATE INDEX  
                                  place_order_by ON place (order_by(50));
        """)
        self.dbapi.try_execute("""CREATE INDEX  
                                  place_gramps_id ON place (gramps_id(50));
        """)
        self.dbapi.try_execute("""CREATE INDEX  
                                  tag_order_by ON tag (order_by(50));
        """)
        self.dbapi.try_execute("""CREATE INDEX  
                                  reference_ref_handle ON reference (ref_handle);
        """)
        self.dbapi.try_execute("""CREATE INDEX  
                                  name_group_name ON name_group (name(50)); 
        """)
        # Load metadata
        self.bookmarks.set(self.get_metadata('bookmarks'))
        self.family_bookmarks.set(self.get_metadata('family_bookmarks'))
        self.event_bookmarks.set(self.get_metadata('event_bookmarks'))
        self.source_bookmarks.set(self.get_metadata('source_bookmarks'))
        self.citation_bookmarks.set(self.get_metadata('citation_bookmarks'))
        self.repo_bookmarks.set(self.get_metadata('repo_bookmarks'))
        self.media_bookmarks.set(self.get_metadata('media_bookmarks'))
        self.place_bookmarks.set(self.get_metadata('place_bookmarks'))
        self.note_bookmarks.set(self.get_metadata('note_bookmarks'))

        # Custom type values
        self.event_names = self.get_metadata('event_names', set())
        self.family_attributes = self.get_metadata('fattr_names', set())
        self.individual_attributes = self.get_metadata('pattr_names', set())
        self.source_attributes = self.get_metadata('sattr_names', set())
        self.marker_names = self.get_metadata('marker_names', set())
        self.child_ref_types = self.get_metadata('child_refs', set())
        self.family_rel_types = self.get_metadata('family_rels', set())
        self.event_role_names = self.get_metadata('event_roles', set())
        self.name_types = self.get_metadata('name_types', set())
        self.origin_types = self.get_metadata('origin_types', set())
        self.repository_types = self.get_metadata('repo_types', set())
        self.note_types = self.get_metadata('note_types', set())
        self.source_media_types = self.get_metadata('sm_types', set())
        self.url_types = self.get_metadata('url_types', set())
        self.media_attributes = self.get_metadata('mattr_names', set())
        self.event_attributes = self.get_metadata('eattr_names', set())
        self.place_types = self.get_metadata('place_types', set())
        
        # surname list
        self.surname_list = self.get_surname_list()

        self.set_save_path(directory)
        self.undolog = os.path.join(self._directory, DBUNDOFN)
        self.undodb = DBAPIUndo(self, self.undolog)
        self.undodb.open()

        # Other items to load
        gstats = self.get_gender_stats()
        self.genderStats = GenderStats(gstats) 

    def set_prefixes(self, person, media, family, source, citation, 
                     place, event, repository, note):
        self.set_person_id_prefix(person)
        self.set_object_id_prefix(media)
        self.set_family_id_prefix(family)
        self.set_source_id_prefix(source)
        self.set_citation_id_prefix(citation)
        self.set_place_id_prefix(place)
        self.set_event_id_prefix(event)
        self.set_repository_id_prefix(repository)
        self.set_note_id_prefix(note)

    def set_save_path(self, directory):
        self._directory = directory
        if directory:
            self.full_name = os.path.abspath(self._directory)
            self.path = self.full_name
            self.brief_name = os.path.basename(self._directory)
        else:
            self.full_name = None
            self.path = None
            self.brief_name = None

    def write_version(self, directory):
        """Write files for a newly created DB."""
        versionpath = os.path.join(directory, str(DBBACKEND))
        _LOG.debug("Write database backend file to 'dbapi'")
        with open(versionpath, "w") as version_file:
            version_file.write("dbapi")
        # Write default_settings, sqlite.db
        defaults = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "dbapi_support", "defaults")
        _LOG.debug("Copy defaults from: " + defaults)
        for filename in os.listdir(defaults):
            fullpath = os.path.abspath(os.path.join(defaults, filename))
            if os.path.isfile(fullpath):
                shutil.copy2(fullpath, directory)

    def report_bm_change(self):
        """
        Add 1 to the number of bookmark changes during this session.
        """
        self._bm_changes += 1

    def db_has_bm_changes(self):
        """
        Return whethere there were bookmark changes during the session.
        """
        return self._bm_changes > 0

    def get_summary(self):
        """
        Returns dictionary of summary item.
        Should include, if possible:

        _("Number of people")
        _("Version")
        _("Schema version")
        """
        return {
            _("Number of people"): self.get_number_of_people(),
        }

    def get_dbname(self):
        """
        In DBAPI, the database is in a text file at the path
        """
        filepath = os.path.join(self._directory, "name.txt")
        try:
            name_file = open(filepath, "r")
            name = name_file.readline().strip()
            name_file.close()
        except (OSError, IOError) as msg:
            _LOG.error(str(msg))
            name = None
        return name

    def reindex_reference_map(self, callback):
        callback(4)
        self.dbapi.execute("DELETE FROM reference;")
        primary_table = (
            (self.get_person_cursor, Person),
            (self.get_family_cursor, Family),
            (self.get_event_cursor, Event),
            (self.get_place_cursor, Place),
            (self.get_source_cursor, Source),
            (self.get_citation_cursor, Citation),
            (self.get_media_cursor, MediaObject),
            (self.get_repository_cursor, Repository),
            (self.get_note_cursor, Note),
            (self.get_tag_cursor, Tag),
        )
        # Now we use the functions and classes defined above
        # to loop through each of the primary object tables.
        for cursor_func, class_func in primary_table:
            logging.info("Rebuilding %s reference map" %
                         class_func.__name__)
            with cursor_func() as cursor:
                for found_handle, val in cursor:
                    obj = class_func.create(val)
                    references = set(obj.get_referenced_handles_recursively())
                    # handle addition of new references
                    for (ref_class_name, ref_handle) in references:
                        self.dbapi.execute("""INSERT INTO reference (obj_handle, obj_class, ref_handle, ref_class)
                                                 VALUES(?, ?, ?, ?);""",
                                           [obj.handle, 
                                            obj.__class__.__name__,
                                            ref_handle, 
                                            ref_class_name])
                                            
        self.dbapi.commit()
        callback(5)

    def rebuild_secondary(self, update):
        gstats = self.rebuild_gender_stats()
        self.genderStats = GenderStats(gstats) 
        self.dbapi.execute("""select blob_data from place;""")
        row = self.dbapi.fetchone()
        while row:
            place = Place.create(pickle.loads(row[0]))
            order_by = self._order_by_place_key(place)
            cur2 = self.dbapi.execute("""UPDATE place SET order_by = ? WHERE handle = ?;""",
                                      [order_by, place.handle])
            row = self.dbapi.fetchone()
        self.dbapi.commit()

    def prepare_import(self):
        """
        Do anything needed before an import.
        """
        pass

    def commit_import(self):
        """
        Do anything needed after an import.
        """
        self.reindex_reference_map(lambda n: n)

    def has_handle_for_person(self, key):
        self.dbapi.execute("SELECT * FROM person WHERE handle = ?", [key])
        return self.dbapi.fetchone() != None

    def has_handle_for_family(self, key):
        self.dbapi.execute("SELECT * FROM family WHERE handle = ?", [key])
        return self.dbapi.fetchone() != None

    def has_handle_for_source(self, key):
        self.dbapi.execute("SELECT * FROM source WHERE handle = ?", [key])
        return self.dbapi.fetchone() != None

    def has_handle_for_citation(self, key):
        self.dbapi.execute("SELECT * FROM citation WHERE handle = ?", [key])
        return self.dbapi.fetchone() != None

    def has_handle_for_event(self, key):
        self.dbapi.execute("SELECT * FROM event WHERE handle = ?", [key])
        return self.dbapi.fetchone() != None

    def has_handle_for_media(self, key):
        self.dbapi.execute("SELECT * FROM media WHERE handle = ?", [key])
        return self.dbapi.fetchone() != None

    def has_handle_for_place(self, key):
        self.dbapi.execute("SELECT * FROM place WHERE handle = ?", [key])
        return self.dbapi.fetchone() != None

    def has_handle_for_repository(self, key):
        self.dbapi.execute("SELECT * FROM repository WHERE handle = ?", [key])
        return self.dbapi.fetchone() != None

    def has_handle_for_note(self, key):
        self.dbapi.execute("SELECT * FROM note WHERE handle = ?", [key])
        return self.dbapi.fetchone() != None

    def has_handle_for_tag(self, key):
        self.dbapi.execute("SELECT * FROM tag WHERE handle = ?", [key])
        return self.dbapi.fetchone() != None

    def has_gramps_id_for_person(self, key):
        self.dbapi.execute("SELECT * FROM person WHERE gramps_id = ?", [key])
        return self.dbapi.fetchone() != None

    def has_gramps_id_for_family(self, key):
        self.dbapi.execute("SELECT * FROM family WHERE gramps_id = ?", [key])
        return self.dbapi.fetchone() != None

    def has_gramps_id_for_source(self, key):
        self.dbapi.execute("SELECT * FROM source WHERE gramps_id = ?", [key])
        return self.dbapi.fetchone() != None

    def has_gramps_id_for_citation(self, key):
        self.dbapi.execute("SELECT * FROM citation WHERE gramps_id = ?", [key])
        return self.dbapi.fetchone() != None

    def has_gramps_id_for_event(self, key):
        self.dbapi.execute("SELECT * FROM event WHERE gramps_id = ?", [key])
        return self.dbapi.fetchone() != None

    def has_gramps_id_for_media(self, key):
        self.dbapi.execute("SELECT * FROM media WHERE gramps_id = ?", [key])
        return self.dbapi.fetchone() != None

    def has_gramps_id_for_place(self, key):
        self.dbapi.execute("SELECT * FROM place WHERE gramps_id = ?", [key])
        return self.dbapi.fetchone() != None

    def has_gramps_id_for_repository(self, key):
        self.dbapi.execute("SELECT * FROM repository WHERE gramps_id = ?", [key])
        return self.dbapi.fetchone() != None

    def has_gramps_id_for_note(self, key):
        self.dbapi.execute("SELECT * FROM note WHERE gramps_id = ?", [key])
        return self.dbapi.fetchone() != None

    def get_person_gramps_ids(self):
        self.dbapi.execute("SELECT gramps_id FROM person;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_family_gramps_ids(self):
        self.dbapi.execute("SELECT gramps_id FROM family;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_source_gramps_ids(self):
        self.dbapi.execute("SELECT gramps_id FROM source;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_citation_gramps_ids(self):
        self.dbapi.execute("SELECT gramps_id FROM citation;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_event_gramps_ids(self):
        self.dbapi.execute("SELECT gramps_id FROM event;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_media_gramps_ids(self):
        self.dbapi.execute("SELECT gramps_id FROM media;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_place_gramps_ids(self):
        self.dbapi.execute("SELECT gramps FROM place;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_repository_gramps_ids(self):
        self.dbapi.execute("SELECT gramps_id FROM repository;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def get_note_gramps_ids(self):
        self.dbapi.execute("SELECT gramps_id FROM note;")
        rows = self.dbapi.fetchall()
        return [row[0] for row in rows]

    def _get_raw_person_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM person WHERE handle = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_person_from_id_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM person WHERE gramps_id = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_family_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM family WHERE handle = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_family_from_id_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM family WHERE gramps_id = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_source_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM source WHERE handle = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_source_from_id_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM source WHERE gramps_id = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_citation_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM citation WHERE handle = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_citation_from_id_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM citation WHERE gramps_id = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_event_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM event WHERE handle = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_event_from_id_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM event WHERE gramps_id = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_media_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM media WHERE handle = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_media_from_id_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM media WHERE gramps_id = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_place_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM place WHERE handle = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_place_from_id_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM place WHERE gramps_id = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_repository_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM repository WHERE handle = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_repository_from_id_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM repository WHERE handle = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_note_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM note WHERE handle = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_note_from_id_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM note WHERE gramps_id = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _get_raw_tag_data(self, key):
        self.dbapi.execute("SELECT blob_data FROM tag WHERE handle = ?", [key])
        row = self.dbapi.fetchone()
        if row:
            return pickle.loads(row[0])

    def _order_by_person_key(self, person):
        """
        All non pa/matronymic surnames are used in indexing.
        pa/matronymic not as they change for every generation!
        returns a byte string
        """
        if person.primary_name and person.primary_name.surname_list:
            order_by = " ".join([x.surname for x in person.primary_name.surname_list if not 
                                 (int(x.origintype) in [NameOriginType.PATRONYMIC, 
                                                        NameOriginType.MATRONYMIC]) ])
        else:
            order_by = ""
        return glocale.sort_key(order_by)

    def _order_by_place_key(self, place):
        return glocale.sort_key(str(int(place.place_type)) + ", " + place.name.value)

    def _order_by_source_key(self, source):
        return glocale.sort_key(source.title)

    def _order_by_citation_key(self, citation):
        return glocale.sort_key(citation.page)

    def _order_by_media_key(self, media):
        return glocale.sort_key(media.desc)

    def _order_by_tag_key(self, tag):
        return glocale.sort_key(tag.get_name())

    def backup(self):
        """
        If you wish to support an optional backup routine, put it here.
        """
        from gramps.plugins.export.exportxml import XmlWriter
        from gramps.cli.user import User 
        writer = XmlWriter(self, User(), strip_photos=0, compress=1)
        filename = os.path.join(self._directory, "data.gramps")
        writer.write(filename)

    def restore(self):
        """
        If you wish to support an optional restore routine, put it here.
        """
        pass

    def get_undodb(self):
        return self.undodb

    def undo(self, update_history=True):
        return self.undodb.undo(update_history)

    def redo(self, update_history=True):
        return self.undodb.redo(update_history)

    def get_dbid(self):
        """
        We use the file directory name as the unique ID for
        this database on this computer.
        """
        return self.brief_name

    def rebuild_gender_stats(self):
        """
        Returns a dictionary of 
        {given_name: (male_count, female_count, unknown_count)} 
        """
        self.dbapi.execute("""SELECT given_name, gender_type FROM person;""")
        gstats = {}
        for row in self.dbapi.fetchall():
            if row[0] not in gstats:
                gstats[row[0]] = [0, 0, 0]
            gstats[row[0]][row[1]] += 1
        return gstats

    def save_gender_stats(self, gstats):
        self.dbapi.execute("""DELETE FROM gender_stats;""")
        for key in gstats.stats:
            female, male, unknown = gstats.stats[key]
            self.dbapi.execute("""INSERT INTO gender_stats(given_name, female, male, unknown) 
                                              VALUES(?, ?, ?, ?);""",
                               [key, female, male, unknown]);
        self.dbapi.commit()

    def get_gender_stats(self):
        """
        Returns a dictionary of 
        {given_name: (male_count, female_count, unknown_count)} 
        """
        self.dbapi.execute("""SELECT given_name, female, male, unknown FROM gender_stats;""")
        gstats = {}
        for row in self.dbapi.fetchall():
            gstats[row[0]] = [row[1], row[2], row[3]]
        return gstats
        
    def get_person_data(self, person):
        """
        Given a Person, return primary_name.first_name, surname and gender.
        """
        given_name = ""
        surname = ""
        gender_type = Person.UNKNOWN
        if person:
            primary_name = person.get_primary_name()
            if primary_name:
                given_name = primary_name.get_first_name()
                surname_list = primary_name.get_surname_list()
                if len(surname_list) > 0:
                    surname_obj = surname_list[0]
                    if surname_obj:
                        surname = surname_obj.surname
            gender_type = person.gender
        return (given_name, surname, gender_type)

    def get_surname_list(self):
        self.dbapi.execute("""SELECT DISTINCT surname FROM person ORDER BY surname;""")
        surname_list = []
        for row in self.dbapi.fetchall():
            surname_list.append(row[0])
        return surname_list

    def save_surname_list(self):
        """
        Save the surname_list into persistant storage.
        """
        # Nothing for DB-API to do; saves in person table
        pass

    def build_surname_list(self):
        """
        Rebuild the surname_list.
        """
        # Nothing for DB-API to do; saves in person table
        pass

    def drop_tables(self):
        self.dbapi.try_execute("""DROP TABLE  person;""")
        self.dbapi.try_execute("""DROP TABLE  family;""")
        self.dbapi.try_execute("""DROP TABLE  source;""")
        self.dbapi.try_execute("""DROP TABLE  citation""")
        self.dbapi.try_execute("""DROP TABLE  event;""")
        self.dbapi.try_execute("""DROP TABLE  media;""")
        self.dbapi.try_execute("""DROP TABLE  place;""")
        self.dbapi.try_execute("""DROP TABLE  repository;""")
        self.dbapi.try_execute("""DROP TABLE  note;""")
        self.dbapi.try_execute("""DROP TABLE  tag;""")
        # Secondary:
        self.dbapi.try_execute("""DROP TABLE  reference;""")
        self.dbapi.try_execute("""DROP TABLE  name_group;""")
        self.dbapi.try_execute("""DROP TABLE  metadata;""")
        self.dbapi.try_execute("""DROP TABLE  gender_stats;""") 
