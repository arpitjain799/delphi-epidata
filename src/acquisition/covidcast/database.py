"""A utility class that handles database operations related to covidcast.

See src/ddl/covidcast.sql for an explanation of each field.
"""

# third party
import json
import mysql.connector
import numpy as np
from math import ceil

from queue import Queue, Empty
import threading
from multiprocessing import cpu_count

# first party
import delphi.operations.secrets as secrets

from delphi.epidata.acquisition.covidcast.logger import get_structured_logger

class CovidcastRow():
  """A container for all the values of a single covidcast row."""

  @staticmethod
  def fromCsvRowValue(row_value, source, signal, time_type, geo_type, time_value, issue, lag):
    if row_value is None: return None
    return CovidcastRow(source, signal, time_type, geo_type, time_value,
                        row_value.geo_value,
                        row_value.value,
                        row_value.stderr,
                        row_value.sample_size,
                        row_value.missing_value,
                        row_value.missing_stderr,
                        row_value.missing_sample_size,
                        issue, lag)

  @staticmethod
  def fromCsvRows(row_values, source, signal, time_type, geo_type, time_value, issue, lag):
    # NOTE: returns a generator, as row_values is expected to be a generator
    return (CovidcastRow.fromCsvRowValue(row_value, source, signal, time_type, geo_type, time_value, issue, lag)
            for row_value in row_values)

  def __init__(self, source, signal, time_type, geo_type, time_value, geo_value, value, stderr, 
               sample_size, missing_value, missing_stderr, missing_sample_size, issue, lag):
    self.id = None
    self.source = source
    self.signal = signal
    self.time_type = time_type
    self.geo_type = geo_type
    self.time_value = time_value
    self.geo_value = geo_value      # from CSV row
    self.value = value              # ...
    self.stderr = stderr            # ...
    self.sample_size = sample_size  # ...
    self.missing_value = missing_value # ...
    self.missing_stderr = missing_stderr # ...
    self.missing_sample_size = missing_sample_size # from CSV row
    self.direction_updated_timestamp = 0
    self.direction = None
    self.issue = issue
    self.lag = lag



class Database:
  """A collection of covidcast database operations."""

  DATABASE_NAME = 'covid'

  load_table = "signal_load"
  latest_table = "signal_latest" # NOTE: careful!  probably want to use variable `latest_view` instead for semantics purposes
  latest_view = latest_table + "_v"
  history_table = "signal_history" # NOTE: careful!  probably want to use variable `history_view` instead for semantics purposes
  history_view = history_table + "_v"
  # TODO: consider using class variables like this for dimension table names too
  # TODO: also consider that for composite key tuples, like short_comp_key and long_comp_key as used in delete_batch()


  def connect(self, connector_impl=mysql.connector):
    """Establish a connection to the database."""

    u, p = secrets.db.epi
    self._connector_impl = connector_impl
    self._connection = self._connector_impl.connect(
        host=secrets.db.host,
        user=u,
        password=p,
        database=Database.DATABASE_NAME)
    self._cursor = self._connection.cursor()

  def commit(self):
    self._connection.commit()

  def rollback(self):
    self._connection.rollback()

  def disconnect(self, commit):
    """Close the database connection.

    commit: if true, commit changes, otherwise rollback
    """

    self._cursor.close()
    if commit:
      self._connection.commit()
    self._connection.close()


  def count_all_rows(self, tablename=None):
    """Return the total number of rows in table `covidcast`."""

    if tablename is None:
      tablename = self.history_table

    self._cursor.execute(f'SELECT count(1) FROM `{tablename}`')

    for (num,) in self._cursor:
      return num

  def count_all_history_rows(self):
    return self.count_all_rows(self.history_table)

  def count_all_latest_rows(self):
    return self.count_all_rows(self.latest_table)

  def count_all_load_rows(self):
    return self.count_all_rows(self.load_table)

  def _reset_load_table_ai_counter(self):
    """Corrects the AUTO_INCREMENT counter in the load table.

    To be used in emergencies only, if the load table was accidentally TRUNCATEd.
    This ensures any `signal_data_id`s generated by the load table will not collide with the history or latest tables.
    This is also destructive to any data in the load table.
    """

    self._cursor.execute(f'DELETE FROM signal_load')
    # NOTE: 'ones' are used as filler here for the (required) NOT NULL columns.
    self._cursor.execute(f"""
      INSERT INTO signal_load
        (signal_data_id,
         source, `signal`, geo_type, geo_value, time_type, time_value, issue, `lag`, value_updated_timestamp)
      VALUES
        ((SELECT 1+MAX(signal_data_id) FROM signal_history),
         '1', '1', '1', '1', '1', 1, 1, 1, 1);""")
    self._cursor.execute(f'DELETE FROM signal_load')

  def insert_or_update_batch(self, cc_rows):
    return self.insert_or_update_batch(cc_rows)

  def insert_or_update_batch(self, cc_rows, batch_size=2**20, commit_partial=False):
    """
    Insert new rows (or update existing) into the load table.
    """

    # NOTE: `value_update_timestamp` is hardcoded to "NOW" (which is appropriate) and 
    #       `is_latest_issue` is hardcoded to 1 (which is temporary and addressed later in this method)
    insert_into_loader_sql = f'''
      INSERT INTO `{self.load_table}`
        (`source`, `signal`, `time_type`, `geo_type`, `time_value`, `geo_value`,
        `value_updated_timestamp`, `value`, `stderr`, `sample_size`, `issue`, `lag`, 
        `is_latest_issue`, `missing_value`, `missing_stderr`, `missing_sample_size`)
      VALUES
        (%s, %s, %s, %s, %s, %s, 
        UNIX_TIMESTAMP(NOW()), %s, %s, %s, %s, %s, 
        1, %s, %s, %s)
    '''

    # all load table entries are already marked "is_latest_issue".
    # if an entry in the load table is NOT in the latest table, it is clearly now the latest value for that key (so we do nothing (thanks to INNER join)).
    # if an entry *IS* in both load and latest tables, but latest table issue is newer, unmark is_latest_issue in load.
    fix_is_latest_issue_sql = f'''
        UPDATE 
            `{self.load_table}` JOIN `{self.latest_view}` 
                USING (`source`, `signal`, `geo_type`, `geo_value`, `time_type`, `time_value`) 
            SET `{self.load_table}`.`is_latest_issue`=0 
            WHERE `{self.load_table}`.`issue` < `{self.latest_view}`.`issue` 
    '''

    if 0 != self.count_all_load_rows():
      # TODO: add a test for this
      logger = get_structured_logger("insert_or_update_batch")
      logger.fatal("Non-zero count in the load table!!!  This indicates a previous acquisition run may have failed, another acquisition is in progress, or this process does not otherwise have exclusive access to the db!")
      raise Exception
    # TODO: consider handling cc_rows as a generator instead of a list

    try:
      num_rows = len(cc_rows)
      total = 0
      if not batch_size:
        batch_size = num_rows
      num_batches = ceil(num_rows/batch_size)
      for batch_num in range(num_batches):
        start = batch_num * batch_size
        end = min(num_rows, start + batch_size)
        length = end - start

        args = [(
          row.source,
          row.signal,
          row.time_type,
          row.geo_type,
          row.time_value,
          row.geo_value,
          row.value,
          row.stderr,
          row.sample_size,
          row.issue,
          row.lag,
          row.missing_value,
          row.missing_stderr,
          row.missing_sample_size
        ) for row in cc_rows[start:end]]


        self._cursor.executemany(insert_into_loader_sql, args)
        modified_row_count = self._cursor.rowcount
        self._cursor.execute(fix_is_latest_issue_sql)
        self.run_dbjobs() # TODO: consider incorporating the logic of dbjobs() into this method [once calls to dbjobs() are no longer needed for migrations]

        if modified_row_count is None or modified_row_count == -1:
          # the SQL connector does not support returning number of rows affected (see PEP 249)
          total = None
        else:
          total += modified_row_count
        if commit_partial:
          self._connection.commit()
    except Exception as e:
      # TODO: rollback???  something???
      raise e
    return total

  def run_dbjobs(self):

    # we do this LEFT JOIN trick because mysql cant do set difference (aka EXCEPT or MINUS)
    # (as in " select distinct source, signal from signal_dim minus select distinct source, signal from signal_load ")
    signal_dim_add_new_load = f'''
        INSERT INTO signal_dim (`source`, `signal`)
            SELECT DISTINCT sl.source, sl.signal
                FROM {self.load_table} AS sl LEFT JOIN signal_dim AS sd
                ON sl.source=sd.source AND sl.signal=sd.signal
                WHERE sd.source IS NULL
    '''

    # again, same trick to get around lack of EXCEPT/MINUS
    geo_dim_add_new_load = f'''
        INSERT INTO geo_dim (`geo_type`, `geo_value`)
            SELECT DISTINCT sl.geo_type, sl.geo_value
                FROM {self.load_table} AS sl LEFT JOIN geo_dim AS gd
                ON sl.geo_type=gd.geo_type AND sl.geo_value=gd.geo_value
                WHERE gd.geo_type IS NULL
    '''

    signal_history_load = f'''
        INSERT INTO {self.history_table}
            (signal_data_id, signal_key_id, geo_key_id, issue, data_as_of_dt,
             time_type, time_value, `value`, stderr, sample_size, `lag`, value_updated_timestamp,
             computation_as_of_dt, missing_value, missing_stderr, missing_sample_size, `legacy_id`)
        SELECT
            signal_data_id, sd.signal_key_id, gd.geo_key_id, issue, data_as_of_dt,
                time_type, time_value, `value`, stderr, sample_size, `lag`, value_updated_timestamp,
                computation_as_of_dt, missing_value, missing_stderr, missing_sample_size, `legacy_id`
            FROM `{self.load_table}` sl
                INNER JOIN signal_dim sd USING (source, `signal`)
                INNER JOIN geo_dim gd USING (geo_type, geo_value)
        ON DUPLICATE KEY UPDATE
            `signal_data_id` = sl.`signal_data_id`,
            `value_updated_timestamp` = sl.`value_updated_timestamp`,
            `value` = sl.`value`,
            `stderr` = sl.`stderr`,
            `sample_size` = sl.`sample_size`,
            `lag` = sl.`lag`,
            `missing_value` = sl.`missing_value`,
            `missing_stderr` = sl.`missing_stderr`,
            `missing_sample_size` = sl.`missing_sample_size`
    '''

    signal_latest_load = f'''
        INSERT INTO {self.latest_table}
            (signal_data_id, signal_key_id, geo_key_id, issue, data_as_of_dt,
             time_type, time_value, `value`, stderr, sample_size, `lag`, value_updated_timestamp,
             computation_as_of_dt, missing_value, missing_stderr, missing_sample_size)
        SELECT
            signal_data_id, sd.signal_key_id, gd.geo_key_id, issue, data_as_of_dt,
                time_type, time_value, `value`, stderr, sample_size, `lag`, value_updated_timestamp,
                computation_as_of_dt, missing_value, missing_stderr, missing_sample_size
            FROM `{self.load_table}` sl
                INNER JOIN signal_dim sd USING (source, `signal`)
                INNER JOIN geo_dim gd USING (geo_type, geo_value)
            WHERE is_latest_issue = 1
        ON DUPLICATE KEY UPDATE
            `signal_data_id` = sl.`signal_data_id`,
            `value_updated_timestamp` = sl.`value_updated_timestamp`,
            `value` = sl.`value`,
            `stderr` = sl.`stderr`,
            `sample_size` = sl.`sample_size`,
            `issue` = sl.`issue`,
            `lag` = sl.`lag`,
            `missing_value` = sl.`missing_value`,
            `missing_stderr` = sl.`missing_stderr`,
            `missing_sample_size` = sl.`missing_sample_size`
    '''

    # NOTE: DO NOT `TRUNCATE` THIS TABLE!  doing so will ruin the AUTO_INCREMENT counter that the history and latest tables depend on...
    signal_load_delete_processed = f'''
        DELETE FROM `{self.load_table}`
    '''

    import time
    time_q = []
    time_q.append(time.time())

    print('signal_dim_add_new_load:', end='')
    self._cursor.execute(signal_dim_add_new_load)
    time_q.append(time.time())
    print(f" elapsed: {time_q[-1]-time_q[-2]}s")

    print('geo_dim_add_new_load:', end='')
    self._cursor.execute(geo_dim_add_new_load)
    time_q.append(time.time())
    print(f" elapsed: {time_q[-1]-time_q[-2]}s")

    print('signal_history_load:', end='')
    self._cursor.execute(signal_history_load)
    time_q.append(time.time())
    print(f" elapsed: {time_q[-1]-time_q[-2]}s")

    print('signal_latest_load:', end='')
    self._cursor.execute(signal_latest_load)
    time_q.append(time.time())
    print(f" elapsed: {time_q[-1]-time_q[-2]}s")

    print('signal_load_delete_processed:', end='')
    self._cursor.execute(signal_load_delete_processed)
    time_q.append(time.time())
    print(f" elapsed: {time_q[-1]-time_q[-2]}s")

    print("done.")

    return self


  def delete_batch(self, cc_deletions):
    """
    Remove rows specified by a csv file or list of tuples.

    If cc_deletions is a filename, the file should include a header row and use the following field order:
    - geo_id
    - value (ignored)
    - stderr (ignored)
    - sample_size (ignored)
    - issue (YYYYMMDD format)
    - time_value (YYYYMMDD format)
    - geo_type
    - signal
    - source

    If cc_deletions is a list of tuples, the tuples should use the following field order (=same as above, plus time_type):
    - geo_id
    - value (ignored)
    - stderr (ignored)
    - sample_size (ignored)
    - issue (YYYYMMDD format)
    - time_value (YYYYMMDD format)
    - geo_type
    - signal
    - source
    - time_type
    """

    tmp_table_name = "tmp_delete_table"
    # composite keys:
    short_comp_key = "`source`, `signal`, `time_type`, `geo_type`, `time_value`, `geo_value`"
    long_comp_key = short_comp_key + ", `issue`"

    create_tmp_table_sql = f'''
CREATE OR REPLACE TABLE {tmp_table_name} LIKE {self.load_table};
'''

    amend_tmp_table_sql = f'''
ALTER TABLE {tmp_table_name} ADD COLUMN delete_history_id BIGINT UNSIGNED,
                             ADD COLUMN delete_latest_id BIGINT UNSIGNED,
                             ADD COLUMN update_latest BINARY(1) DEFAULT 0;
'''

    load_tmp_table_infile_sql = f'''
LOAD DATA INFILE "{cc_deletions}"
INTO TABLE {tmp_table_name}
FIELDS TERMINATED BY ","
IGNORE 1 LINES
(`geo_value`, `value`, `stderr`, `sample_size`, `issue`, `time_value`, `geo_type`, `signal`, `source`)
SET time_type="day";
'''

    load_tmp_table_insert_sql = f'''
INSERT INTO {tmp_table_name}
(`geo_value`, `value`, `stderr`, `sample_size`, `issue`, `time_value`, `geo_type`, `signal`, `source`, `time_type`,
`value_updated_timestamp`, `lag`, `is_latest_issue`)
VALUES
(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
0, 0, 0)
'''

    add_history_id_sql = f'''
UPDATE {tmp_table_name} d INNER JOIN {self.history_view} h USING ({long_comp_key})
SET d.delete_history_id=h.signal_data_id;
'''

    # if a row we are deleting also appears in the 'latest' table (with a matching 'issue')...
    mark_for_update_latest_sql = f'''
UPDATE {tmp_table_name} d INNER JOIN {self.latest_view} ell USING ({long_comp_key})
SET d.update_latest=1, d.delete_latest_id=ell.signal_data_id;
'''

    delete_history_sql = f'''
DELETE h FROM {tmp_table_name} d INNER JOIN {self.history_table} h ON d.delete_history_id=h.signal_data_id;
'''

    # ...remove it from 'latest'...
    delete_latest_sql = f'''
DELETE ell FROM {tmp_table_name} d INNER JOIN {self.latest_table} ell ON d.delete_latest_id=ell.signal_data_id;
'''

    # ...and re-write that record with its next-latest issue (from 'history') instead.
    # NOTE: this must be executed *AFTER* `delete_history_sql` to ensure we get the correct `issue`
    #       AND also after `delete_latest_sql` so that we dont get a key collision on insert.
    update_latest_sql = f'''
INSERT INTO {self.latest_table}
  (issue,
  signal_data_id, signal_key_id, geo_key_id, time_type, time_value,
  value, stderr, sample_size, `lag`, value_updated_timestamp,
  missing_value, missing_stderr, missing_sample_size)
SELECT
  MAX(h.issue),
  h.signal_data_id, h.signal_key_id, h.geo_key_id, h.time_type, h.time_value,
  h.value, h.stderr, h.sample_size, h.`lag`, h.value_updated_timestamp,
  h.missing_value, h.missing_stderr, h.missing_sample_size
FROM {self.history_view} h JOIN {tmp_table_name} d USING ({short_comp_key})
WHERE d.update_latest=1 GROUP BY {short_comp_key};
'''

    drop_tmp_table_sql = f'DROP TABLE {tmp_table_name}'

    total = None
    try:
      self._cursor.execute(create_tmp_table_sql)
      self._cursor.execute(amend_tmp_table_sql)
      if isinstance(cc_deletions, str):
        self._cursor.execute(load_tmp_table_infile_sql)
      elif isinstance(cc_deletions, list):
        def split_list(lst, n):
          for i in range(0, len(lst), n):
            yield lst[i:(i+n)]
        for deletions_batch in split_list(cc_deletions, 100000):
          self._cursor.executemany(load_tmp_table_insert_sql, deletions_batch)
      else:
        raise Exception(f"Bad deletions argument: need a filename or a list of tuples; got a {type(cc_deletions)}")
      self._cursor.execute(add_history_id_sql)
      self._cursor.execute(mark_for_update_latest_sql)
      self._cursor.execute(delete_history_sql)
      total = self._cursor.rowcount
      # TODO: consider reporting rows removed and/or replaced in latest table as well
      self._cursor.execute(delete_latest_sql)
      self._cursor.execute(update_latest_sql)
      self._connection.commit()

      if total == -1:
        # the SQL connector does not support returning number of rows affected (see PEP 249)
        total = None
    except Exception as e:
      raise e
    finally:
      self._cursor.execute(drop_tmp_table_sql)
    return total


  def compute_covidcast_meta(self, table_name=None):
    """Compute and return metadata on all COVIDcast signals."""
    logger = get_structured_logger("compute_covidcast_meta")

    if table_name is None:
      table_name = self.latest_view

    n_threads = max(1, cpu_count()*9//10) # aka number of concurrent db connections, which [sh|c]ould be ~<= 90% of the #cores available to SQL server
    # NOTE: this may present a small problem if this job runs on different hardware than the db,
    #       but we should not run into that issue in prod.
    logger.info(f"using {n_threads} workers")

    srcsigs = Queue() # multi-consumer threadsafe!
    sql = f'SELECT `source`, `signal` FROM `{table_name}` GROUP BY `source`, `signal` ORDER BY `source` ASC, `signal` ASC;'
    self._cursor.execute(sql)
    for source, signal in self._cursor:
      srcsigs.put((source, signal))

    inner_sql = f'''
      SELECT
        `source` AS `data_source`,
        `signal`,
        `time_type`,
        `geo_type`,
        MIN(`time_value`) AS `min_time`,
        MAX(`time_value`) AS `max_time`,
        COUNT(DISTINCT `geo_value`) AS `num_locations`,
        MIN(`value`) AS `min_value`,
        MAX(`value`) AS `max_value`,
        ROUND(AVG(`value`),7) AS `mean_value`,
        ROUND(STD(`value`),7) AS `stdev_value`,
        MAX(`value_updated_timestamp`) AS `last_update`,
        MAX(`issue`) as `max_issue`,
        MIN(`lag`) as `min_lag`,
        MAX(`lag`) as `max_lag`
      FROM
        `{table_name}`
      WHERE
        `source` = %s AND
        `signal` = %s
      GROUP BY
        `time_type`,
        `geo_type`
      ORDER BY
        `time_type` ASC,
        `geo_type` ASC
      '''

    meta = []
    meta_lock = threading.Lock()

    def worker():
      name = threading.current_thread().name
      logger.info("starting thread", thread=name)
      #  set up new db connection for thread
      worker_dbc = Database()
      worker_dbc.connect(connector_impl=self._connector_impl)
      w_cursor = worker_dbc._cursor
      try:
        while True:
          (source, signal) = srcsigs.get_nowait() # this will throw the Empty caught below
          logger.info("starting pair", thread=name, pair=f"({source}, {signal})")
          w_cursor.execute(inner_sql, (source, signal))
          with meta_lock:
            meta.extend(list(
              dict(zip(w_cursor.column_names, x)) for x in w_cursor
            ))
          srcsigs.task_done()
      except Empty:
        logger.info("no jobs left, thread terminating", thread=name)
      finally:
        worker_dbc.disconnect(False) # cleanup

    threads = []
    for n in range(n_threads):
      t = threading.Thread(target=worker, name='MetacacheThread-'+str(n))
      t.start()
      threads.append(t)

    srcsigs.join()
    logger.info("jobs complete")
    for t in threads:
      t.join()
    logger.info("all threads terminated")

    # sort the metadata because threaded workers dgaf
    sorting_fields = "data_source signal time_type geo_type".split()
    sortable_fields_fn = lambda x: [(field, x[field]) for field in sorting_fields]
    prepended_sortables_fn = lambda x: sortable_fields_fn(x) + list(x.items())
    tuple_representation = list(map(prepended_sortables_fn, meta))
    tuple_representation.sort()
    meta = list(map(dict, tuple_representation)) # back to dict form

    return meta


  def update_covidcast_meta_cache(self, metadata):
    """Updates the `covidcast_meta_cache` table."""

    sql = '''
      UPDATE
        `covidcast_meta_cache`
      SET
        `timestamp` = UNIX_TIMESTAMP(NOW()),
        `epidata` = %s
    '''
    epidata_json = json.dumps(metadata)

    self._cursor.execute(sql, (epidata_json,))

  def retrieve_covidcast_meta_cache(self):
    """Useful for viewing cache entries (was used in debugging)"""

    sql = '''
      SELECT `epidata`
      FROM `covidcast_meta_cache`
      ORDER BY `timestamp` DESC
      LIMIT 1;
    '''
    self._cursor.execute(sql)
    cache_json = self._cursor.fetchone()[0]
    cache = json.loads(cache_json)
    cache_hash = {}
    for entry in cache:
      cache_hash[(entry['data_source'], entry['signal'], entry['time_type'], entry['geo_type'])] = entry
    return cache_hash
