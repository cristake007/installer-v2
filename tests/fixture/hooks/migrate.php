<?php
$url=parse_url(getenv('DATABASE_URL'));
$db=new PDO('pgsql:host='.$url['host'].';dbname='.ltrim($url['path'],'/'), $url['user'],$url['pass'],[PDO::ATTR_ERRMODE=>PDO::ERRMODE_EXCEPTION]);
$db->exec('CREATE TABLE IF NOT EXISTS migration_probe (id integer)');
if (trim(file_get_contents('/opt/installer/fail')) === '1') { $db->exec('CREATE SCHEMA failed_upgrade; CREATE TABLE failed_upgrade.debris (id integer); DELETE FROM persistence'); exit(42); }
