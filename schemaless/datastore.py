import time
import simplejson
import zlib

import tornado.database

from schemaless.column import Entity
from schemaless.index import Index
from schemaless.guid import raw_guid
from schemaless.log import ClassLogger

class DataStore(object):

    log = ClassLogger()

    def __init__(self, mysql_shards=[], user=None, database=None, password=None, use_zlib=True, indexes=[]):
        if not mysql_shards:
            raise ValueError('Must specify at least one MySQL shard')
        if len(mysql_shards) > 1:
            raise NotImplementedError
        self.use_zlib = use_zlib
        self.indexes = []
        self.connection = tornado.database.Connection(host=mysql_shards[0], user=user, password=password, database=database)

    def define_index(self, table, properties=[], match_on={}, shard_on=None):
        idx = Index(table=table, properties=properties, match_on=match_on, shard_on=shard_on, connection=self.connection, use_zlib=self.use_zlib)
        self.indexes.append(idx)
        return idx

    def _find_indexes(self, entity):
        """Find all of the indexes that may index an entity, based on the keys
        in the entity.
        """
        keys = frozenset(entity.keys())
        for idx in self.indexes:
            if idx.matches(entity, keys):
                yield idx
    
    def put(self, entity):
        is_update = False
        entity['updated'] = time.time()
        entity_id = None

        entity_copy = entity.copy()

        # get the entity_id (or create a new one)
        entity_id = entity_copy.pop('id', None)
        if entity_id is None:
            entity_id = raw_guid()
        else:
            is_update = True
            if len(entity_id) != 16:
                entity_id = entity_id.decode('hex')
        body = simplejson.dumps(entity_copy)
        if self.use_zlib:
            body = zlib.compress(body, 1)

        if is_update:
            self._put_update(entity_id, entity_copy, body)
            return entity
        else:
            return self._put_new(entity_id, entity_copy, body)

    def _put_new(self, entity_id, entity, body):
        pk = self.connection.execute('INSERT INTO entities (id, updated, body) VALUES (%s, FROM_UNIXTIME(%s), %s)', entity_id, int(entity['updated']), body)

        indexes = []
        for idx in self._find_indexes(entity):
            pnames = ['entity_id']
            v = [entity_id]
            for p in idx.properties:
                pnames.append(p)
                v.append(entity[p])

            q = 'INSERT INTO %s (%s) VALUES (' % (idx.table, ', '.join(pnames))
            q += ', '.join('%s' for x in pnames)
            q += ')'
            try:
                self.connection.execute(q, *v)
            except:
                self.log.exception('Failed to execute _put_new query %r, vals = %r' % (q, v))
                raise

        return self.by_added_id(pk)

    def _put_update(self, entity_id, entity, body):
        self.connection.execute('UPDATE entities SET updated = CURRENT_TIMESTAMP, body = %s WHERE id = %s', body, entity_id)

        indexes = []
        for idx in self._find_indexes(entity):
            vals = []
            q = 'UPDATE %s SET ' % idx.table
            qs = []
            for p in idx.properties:
                qs.append('%s = %%s' % p)
                vals.append(entity[p])
            q += ', '.join(qs)
            q += ' WHERE entity_id = %s'
            vals.append(entity_id)
            try:
                self.connection.execute(q, *vals)
            except:
                self.log.exception('Failed to execute _put_update query %r, vals = %r' % (q, vals))
                raise

    def delete(self, entity=None, id=None):
        if entity is None and id is None:
            raise ValueError('Must provide delete with an entity and an id')
        if entity and 'id' not in entity:
            raise ValueError('Cannot provide an entity without an id')
        if not entity:
            entity = self.by_id(id)
            if not entity:
                return 0
        entity_id = entity['id'].decode('hex')

        def _delete(table_name, col):
            return int(bool(self.connection.execute('DELETE FROM %s WHERE %s = %%s' % (table_name, col), entity_id)))

        deleted = 0
        for idx in self._find_indexes(entity):
            deleted += _delete(idx.table, 'entity_id')
        deleted += _delete('entities', 'id')
        return deleted

    def by_id(self, id):
        if len(id) == 32:
            id = id.decode('hex')
        row = self.connection.get('SELECT * FROM entities WHERE id = %s', id)
        return Entity.from_row(row, use_zlib=self.use_zlib) if row else None

    def by_added_id(self, added_id):
        row = self.connection.get('SELECT * FROM entities WHERE added_id = %s', added_id)
        return Entity.from_row(row, use_zlib=self.use_zlib)
