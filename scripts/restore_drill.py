"""Validate an isolated PostgreSQL restore; never overwrites an existing populated target."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
import psycopg
from psycopg import sql

p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--target',required=True);p.add_argument('--output',required=True);p.add_argument('--bin-dir',default='');a=p.parse_args()
if a.source==a.target or not a.target.startswith('billing_security_restore'):
 raise SystemExit('Use a separate billing_security_restore* database, never the source or a production target.')
config={'host':os.environ['DB_HOST'],'port':os.environ.get('DB_PORT','5432'),'user':os.environ['DB_USER'],'password':os.environ['DB_PASSWORD']}
if config['host'] not in ('127.0.0.1','localhost'):
 raise SystemExit('This development drill supports loopback test databases only.')
with psycopg.connect(**config,dbname=a.target) as c:
 if c.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'").fetchone()[0]:raise SystemExit('Target is not empty; prepare a new isolated restore database.')
def manifest(name):
 result={}
 with psycopg.connect(**config,dbname=name) as c:
  tables=c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
  for (table,) in tables:
   # Hash complete canonical row JSON, including auth, history, notes and snapshots;
   # write only digests and counts to the evidence artifact.
   digest=hashlib.sha256();count=0
   for (row,) in c.execute(sql.SQL('SELECT row_to_json(t)::text FROM {} t ORDER BY row_to_json(t)::text').format(sql.Identifier(table))):
    digest.update(row.encode());digest.update(b'\n');count+=1
   result[table]={'rows':count,'sha256':digest.hexdigest()}
  schema={
   'columns':c.execute("SELECT table_name,column_name,row_number() OVER (PARTITION BY table_name ORDER BY ordinal_position),data_type,character_maximum_length,numeric_precision,numeric_scale,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position").fetchall(),
   'constraints':c.execute("SELECT c.relname,k.conname,pg_get_constraintdef(k.oid),k.convalidated FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' ORDER BY c.relname,k.conname").fetchall(),
   'indexes':c.execute("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY tablename,indexname").fetchall(),
  }
  # pg_dump compacts dropped-column ordinal gaps and PostgreSQL reparses
  # equivalent ANY(text[]) casts. Canonicalize only that exact cast shape.
  def index_form(value):
   return re.sub(r"\(ARRAY\[([^]]+)\]\)::text\[\]",lambda match:'ARRAY['+re.sub(r"('(?:[^']|'')*'::character varying)",r"(\1)::text",match[1])+']',value)
  schema['indexes']=[(table,name,index_form(definition)) for table,name,definition in schema['indexes']]
  result['_schema']={'sha256':hashlib.sha256(json.dumps(schema,sort_keys=True,default=str).encode()).hexdigest(),'columns':len(schema['columns']),'constraints':len(schema['constraints']),'indexes':len(schema['indexes']),'all_constraints_validated':all(row[3] for row in schema['constraints'])}
 return result
before=manifest(a.source);started=time.perf_counter()
env=dict(os.environ,PGHOST=config['host'],PGPORT=config['port'],PGUSER=config['user'],PGPASSWORD=config['password'])
def binary(name):return str(Path(a.bin_dir)/name) if a.bin_dir else name
with tempfile.TemporaryDirectory(prefix='billing-isolated-restore-') as temporary:
 dump=Path(temporary)/'backup.dump'
 subprocess.run([binary('pg_dump'),'-Fc','--no-owner','-f',str(dump),a.source],env=env,check=True)
 subprocess.run([binary('pg_restore'),'--list',str(dump)],env=env,check=True,stdout=subprocess.DEVNULL)
 subprocess.run([binary('pg_restore'),'--no-owner','--no-acl','--exit-on-error','-d',a.target,str(dump)],env=env,check=True)
 checksum=hashlib.sha256(dump.read_bytes()).hexdigest();size=dump.stat().st_size
elapsed=time.perf_counter()-started;after=manifest(a.target)
result={'synthetic':True,'source':a.source,'target':a.target,'matched':before==after,'dump_sha256':checksum,'dump_bytes':size,'recovery_seconds':round(elapsed,3),'tables':after,
 'production_backup_verified':False,'agreed_rpo':None,'agreed_rto':None,'note':'Local synthetic drill only. Production backup access, retention and agreed recovery objectives remain pending.'}
Path(a.output).write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({key:result[key] for key in ('matched','dump_bytes','recovery_seconds','production_backup_verified')}))
if before!=after:raise SystemExit('Restore record/schema manifest mismatch')
