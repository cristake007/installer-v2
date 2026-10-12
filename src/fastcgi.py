"""Minimal bounded FastCGI health request; never expose FastCGI publicly."""
import socket
import struct


def probe(port):
    def record(kind, body):
        return struct.pack('!BBHHBB',1,kind,1,len(body),0,0)+body
    params = {'SCRIPT_FILENAME':'/var/www/html/public/health.php','SCRIPT_NAME':'/health.php','REQUEST_METHOD':'GET','SERVER_PROTOCOL':'HTTP/1.1','REMOTE_ADDR':'127.0.0.1','SERVER_NAME':'localhost','SERVER_PORT':'80'}
    body=b''.join(bytes((len(k),len(v)))+k.encode()+v.encode() for k,v in params.items())
    with socket.create_connection(('127.0.0.1',port),timeout=5) as stream:
        stream.sendall(record(1,struct.pack('!HB5x',1,0))+record(4,body)+record(4,b'')+record(5,b''))
        output=b''
        def exact(n):
            data=b''
            while len(data)<n:
                chunk=stream.recv(n-len(data))
                if not chunk: raise OSError('FastCGI closed before completing health request')
                data+=chunk
            return data
        for _ in range(128):
            version, kind, request, length, padding, _ = struct.unpack('!BBHHBB',exact(8))
            data=exact(length);exact(padding)
            if version!=1 or request!=1: raise OSError('Invalid FastCGI response')
            if kind==6: output+=data
            if len(output)>65536: raise OSError('Oversized FastCGI response')
            if kind==3:
                status, protocol = struct.unpack('!IB3x',data)
                if status or protocol: raise OSError('FastCGI application failed')
                headers, separator, response=output.partition(b'\r\n\r\n')
                if not separator or b'Status: 5' in headers or b'Status: 4' in headers or response.strip()!=b'PHP 8.4 OK':
                    raise OSError('FastCGI health endpoint failed')
                return
        raise OSError('FastCGI response did not complete')
