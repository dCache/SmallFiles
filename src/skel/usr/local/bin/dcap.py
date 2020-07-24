#!/usr/bin/env python3

from urllib.parse import urlparse
import socket
import struct
import os
import sys

VER_MAJ = 0
VER_MIN = 0

# protocol constants

END_OF_DATA =  0xffffffff
DCAP_WRITE = 1
DCAP_READ = 2
DCAP_SEEK = 3
DCAP_CLOSE = 4
DCAP_READV = 13;
DATA = 8
DCAP_SEEK_SET = 0
DCAP_SEEK_CUR = 1
DCAP_SEEK_END = 2

def _merge_string(b):
	r = ''
	for s in b:
		r = r + ' ' + s
	return r

class Dcap:
	"""dCache Client Access Protocol DCAP"""

	def __init__(self, url):
		u = urlparse(url)
		self.host = u.hostname
		self.port = u.port
		self.root =u.path
		self.seq = 0
		self._connect()
		self._send_hello()

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.close()

	def _connect(self):
		self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.socket.connect((self.host, self.port))

	def _close(self):
		self.socket.close()

	def _rcv_control_msg(self):
		msg = ''
		while True:
			chunk = self.socket.recv(1).decode('utf-8')
			if chunk == '':
				raise RuntimeError("socket connection broken")
			if chunk == '\n':
				break
			msg = msg + chunk
		return msg

	def _send_control_msg(self, msg):
		cmsg = msg + '\n'
		sent = self.socket.sendall(cmsg.encode('utf-8'))

		if sent == 0:
			raise RuntimeError("socket connection broken")
		self.seq += 1

	def _send_hello(self):
		hello = "%d 0 client hello %d %d %d %d" % (self.seq, VER_MAJ, VER_MIN, VER_MAJ, VER_MIN)
		self._send_control_msg(hello)
		reply = self._rcv_control_msg()

	def _send_bye(self):
		bye = "%d 0 client byebye" % (self.seq)
		self._send_control_msg(bye)
		reply = self._rcv_control_msg()

	def open_file(self, path, mode='r'):
		session = self.seq
		open_opemmand = "%d 0 client open \"dcap://%s:%d/%s/%s\" %s localhost 1111 -passive -uid=%d -gid=%d -mode=0644" % \
			(self.seq, self.host, self.port, self.root, path, mode, os.getuid(), os.getgid())
		self._send_control_msg(open_opemmand)
		reply = self._rcv_control_msg()
		host, port, chalange = self.parse_reply(reply, path)

		data_socket = self._init_data_connection(session, host, port, chalange)
		return DcapStream(data_socket, self)

	def parse_reply(self, reply, path):
		s = reply.split()
		if s[3] == 'failed':
			raise RuntimeError("failed to open file " + path + ": " + _merge_string(s[5:]))
		return s[4], int(s[5]), s[6]

	def _init_data_connection(self, session, host, port, chalange):
		s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		s.connect((host, port))

		packer = struct.Struct('>II')

		s.send(packer.pack(session, len(chalange)))
		s.send(chalange.encode('utf-8'))
		return s

	def rename(self, src, dest):
		rename_cmd = "%d 0 client rename dcap://%s:%d/%s/%s %s" % \
			(self.seq, self.host, self.port, self.root, src, dest )
		self._send_control_msg(rename_cmd)
		reply = self._rcv_control_msg()

	def close(self):
		self._send_bye()
		self._close()

def readFully(s, count):
	n = count
	data = bytearray()
	while n > 0:
		d = s.recv(n)
		n = n - len(d)
		data.extend(d)
	return data

class DcapStream:

	def __init__(self, sock, dcap):
		self.socket = sock
		self.dcap = dcap

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.close()

	def _get_ack(self):
		unpacker = struct.Struct('>I')
		msg = self.socket.recv(unpacker.size)
		size = unpacker.unpack(msg)[0]
		msg = self.socket.recv(size)
		return msg

	def _get_data(self):
		unpacker = struct.Struct('>II')
		msg = self.socket.recv(unpacker.size)

		data_unpacker = struct.Struct('>I')
		data = bytearray()
		while True:
			data_header = self.socket.recv(data_unpacker.size)
			count = data_unpacker.unpack(data_header)[0]

			if count == END_OF_DATA:
				self._get_ack()
				break
			data.extend(readFully(self.socket, count))
		return data


	def read(self, count = -1):

		if count == -1:
			data = bytearray()
			while True:
				d = self.read(1024*1024)
				if len(d) == 0:
					break;
				data.extend(d)
			return data

		packer = struct.Struct('>IIq')
		msg = packer.pack( 12, DCAP_READ, count)
		self.socket.sendall(msg)
		self._get_ack()
		return self._get_data()

	def seek(self, offset,  from_what = 0 ):
		packer = struct.Struct('>IIqI')
		msg = packer.pack( 16, DCAP_SEEK, offset,  from_what );
		self.socket.sendall(msg)
		cb = self._get_ack()
		unpacker = struct.Struct('>IIIq')
		return unpacker.unpack(cb)[3]

	def tell(self):
		return self.seek(0, DCAP_SEEK_CUR)

	def flush(self):
		pass

	def parse_reply(self, reply):
		s = reply.split()
		if s[3] == 'failed':
			raise RuntimeError("failed to close file: " + _merge_string(s[4:]))


	def close(self):
		packer = struct.Struct('>II')

		msg = packer.pack(4, DCAP_CLOSE)
		self.socket.sendall(msg)
		self._get_ack()
		reply = self.dcap._rcv_control_msg()
		self.socket.close()
		self.parse_reply(reply)

	def send_file(self, src):

		statinfo = os.stat(src)
		packer = struct.Struct('>II')
		msg = packer.pack(4, DCAP_WRITE)
		self.socket.sendall(msg)
		self._get_ack()

		data_packer = struct.Struct('>III')
		data_header = data_packer.pack(4, DATA, statinfo.st_size)
		self.socket.sendall(data_header)

		with open(src,'rb') as f:
			while True:
				data = f.read(256 * 1024)
				if len(data) == 0:
					break
				self.socket.sendall(data)

		data_packer = struct.Struct('>I')
		data_header = data_packer.pack(END_OF_DATA)
		self.socket.sendall(data_header)
		self._get_ack()

	def recv_file(self, dst):

		with open(dst,'wb') as f:
			while True:
				data = self.read(1024 * 1024)
				if len(data) == 0:
					break
				f.write(data)

	def write(self, buf):
		packer = struct.Struct('>II')
		msg = packer.pack(4, DCAP_WRITE)
		self.socket.sendall(msg)
		self._get_ack()

		data_packer = struct.Struct('>III')
		data_header = data_packer.pack(4, DATA, len(buf))
		self.socket.sendall(data_header)
		self.socket.sendall(buf)
		data_packer = struct.Struct('>I')
		data_header = data_packer.pack(END_OF_DATA)
		self.socket.sendall(data_header)
		self._get_ack()

	def readv(self, iovecs):
		n = 8+len(iovecs)*12
		packer = struct.Struct('>III')
		msg = packer.pack(n, DCAP_READV, len(iovecs))
		self.socket.sendall(msg)

		totalToRead = 0
		data_packer = struct.Struct('>QI')
		for o, l in iovecs:
			totalToRead = totalToRead + l
			chunk = data_packer.pack(o, l)
			self.socket.sendall(chunk)
			self._get_ack()

		data = ''
		data_unpacker = struct.Struct('>I')
		while totalToRead > 0:
			data_header = self.socket.recv(data_unpacker.size)
			count = data_unpacker.unpack(data_header)[0]
			print("reading %d bytes" % count)
			data = data + readFully(self.socket, count)
			totalToRead = totalToRead - count
		self._get_ack()
		return data

def usage_and_exit():
	print("Usage: dcap <PUT|GET> <door> <local file> <remote file>")
	sys.exit(1)


if __name__ == "__main__":

	if len(sys.argv) < 5:
		usage_and_exit()

	op = sys.argv[1]
	door = sys.argv[2]
	local = sys.argv[3]
	remote = sys.argv[4]

	if op == "PUT" :
		with Dcap(door) as dcap:
			with dcap.open_file(remote, 'w') as f:
				f.send_file(local)

	elif op == "GET":
		with Dcap(door) as dcap:
			with dcap.open_file(remote, 'r') as f:
				f.recv_file(local)
	else:
		usage_and_exit()


