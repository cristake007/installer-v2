<?php
if (trim(file_get_contents('/opt/installer/unhealthy')) === '1') { http_response_code(503); exit(1); }
try {
    $url = parse_url(getenv('DATABASE_URL'));
    $db = new PDO('pgsql:host='.$url['host'].';dbname='.ltrim($url['path'],'/'), $url['user'], $url['pass'], [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
    $db->query('SELECT 1');
    $redis = stream_socket_client('tcp://redis:6379', $errno, $error, 3);
    if (!$redis) throw new Exception($error);
    fwrite($redis, "*1\r\n$4\r\nPING\r\n");
    if (trim(fgets($redis)) !== '+PONG') throw new Exception('Redis unavailable');
    echo "healthy\n";
} catch (Throwable $e) { http_response_code(503); error_log($e->getMessage()); exit(1); }
