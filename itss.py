#!/usr/bin/env python3.7
import pprint
import struct
import datetime
import argparse
import sys
import os
import asyncio
import traceback

import requests

import cryptography.hazmat.backends
import cryptography.hazmat.primitives.asymmetric.ec
import cryptography.hazmat.primitives.hashes
import cryptography.hazmat.primitives.asymmetric.utils
import cryptography.hazmat.primitives.serialization

import asn1tools

import itss


class ITSS(object):


	fname = './ts_102941_v111.asn'
	asn1 = asn1tools.compile_files(fname, 'der')


	def __init__(self, directory, ea_url, aa_url):
		self.PrivateKey = None
		self.EC = None # Enrollment credentials
		self.AT = None # Authorization ticket

		self.Directory = directory
		self.AA_url = aa_url
		self.EA_url = ea_url

		self.Certs = {}

		cert_dir = os.path.join(self.Directory, "certs")
		if not os.path.isdir(cert_dir):
			os.mkdir(cert_dir)

		self.Engine = None

		# Initialize HSM engine
		print("Loading HSM engine")
		backend = cryptography.hazmat.backends.default_backend()
		backend._lib.ENGINE_load_dynamic()
		engine = backend._lib.ENGINE_by_id(b"dynamic");
		backend.openssl_assert(engine != backend._ffi.NULL)
		engine = backend._ffi.gc(engine, backend._lib.ENGINE_free)

		backend._lib.ENGINE_ctrl_cmd_string(engine, b"SO_PATH", b"/usr/lib/arm-linux-gnueabihf/engines-1.1/libpkcs11.so", 0)
		backend._lib.ENGINE_ctrl_cmd_string(engine, b"ID", b"pkcs11", 0)
		backend._lib.ENGINE_ctrl_cmd_string(engine, b"LOAD", backend._ffi.NULL, 0)
		backend._lib.ENGINE_ctrl_cmd_string(engine, b"MODULE_PATH", b"/usr/lib/cicada-pkcs11.so", 0)
		res = backend._lib.ENGINE_init(engine)
		backend.openssl_assert(res > 0)

		self.Engine = engine


	def generate_private_key(self):
		if self.Engine is None:
			self.PrivateKey = cryptography.hazmat.primitives.asymmetric.ec.generate_private_key(
				cryptography.hazmat.primitives.asymmetric.ec.SECP256R1(),
				cryptography.hazmat.backends.default_backend()
			)
		else:
			raise RuntimeError("Not implemented yet!")

		self.EC = None
		self.AT = None


	def enroll(self):
		'''
		Send Enrollment request to Enrollment Authority and process the response.
		The process is described in CITS / ETSI TS 102 941 V1.1.1
		'''

		verification_public_key = self.PrivateKey.public_key()
		verification_public_numbers = verification_public_key.public_numbers()

		response_encryption_private_key = cryptography.hazmat.primitives.asymmetric.ec.generate_private_key(
			cryptography.hazmat.primitives.asymmetric.ec.SECP256R1(),
			cryptography.hazmat.backends.default_backend()
		)

		response_encryption_public_key = response_encryption_private_key.public_key()
		response_encryption_public_numbers = response_encryption_public_key.public_numbers()

		requestTime = int((datetime.datetime.utcnow() - datetime.datetime(2004,1,1)).total_seconds())
		expiration = requestTime +  3600 # 1 hour

		EnrolmentRequest = \
		{
			# The canonical certificate or the public/private key pair that uniquely identifies the ITS-S
			'signerEnrolRequest': 
			{
				'type': 3, # SignerIdType.certificate (fixed)
				'digest': b'12345678',
				'id': b'This is the identification of the signer', # Can be used to link request with a preauthorization at EA
			},
			'enrolCertRequest':
			{
				'versionAndType': 2, # explicitCert (fixed)
				'requestTime': requestTime,
				'subjectType': 3, # SecDataExchCsr (fixed)
				'cf': (b'\0',0), # useStartValidity, shall not be set to include the encryption_key flag
				'enrolCertSpecificData': {
					'eaId': 'EAName', # Name (What to set?)
					'permittedSubjectTypes': 0, # secDataExchAnonymousSubj (or secDataExchidentifiedLocalizedSubj)
					'permissions': { # PsidSspArray
						'type': 1, # ArrayType / specified
						'permissions-list': [] # shall contain a list of the ETSI ITS-AIDs to be supported.
					},
					'region': {
						'region-type': 0 # RegionType / from-issuer
					}
				},
				'expiration': requestTime,
				'verificationKey': {
					'algorithm': 1, # PKAlgorithm ecdsaNistp256WithSha256 (fixed)
					'public-key': {
						'type': 'uncompressed', # EccPublicKeyType uncompressed
						'x': (
							'ecdsa-nistp256-with-sha256-X',
							verification_public_numbers.x
						),
						'y': (
							'ecdsa-nistp256-with-sha256-Y',
							verification_public_numbers.y
						)
					}
				},
				'responseEncryptionKey': {
					'algorithm': 1, # PKAlgorithm ecdsaNistp256WithSha256
					'public-key': {
						'type': 'compressedLsbY0', # EccPublicKeyType compressedLsbY0
						'x': (
							'ecdsa-nistp256-with-sha256-X',
							response_encryption_public_numbers.x,
						)
					}
				},
			}
		}

		encoded_er = b''
		encoded_er += struct.pack(">B", 0xa0) + self.asn1.encode('SignerIdentifier', EnrolmentRequest['signerEnrolRequest'])[1:]
		encoded_er += struct.pack(">B", 0xa1) + self.asn1.encode('ToBeSignedEnrolmentCertificateRequest', EnrolmentRequest['enrolCertRequest'])[1:]

		# Sign with ecdsa_nistp256_with_sha256
		signature_RFC3279 = self.PrivateKey.sign(
			encoded_er,
			cryptography.hazmat.primitives.asymmetric.ec.ECDSA(
				cryptography.hazmat.primitives.hashes.SHA256()
			)
		)
		r, s = cryptography.hazmat.primitives.asymmetric.utils.decode_dss_signature(signature_RFC3279)

		EnrolmentRequest['signature'] = {
			'r': {
				'type': 'xCoordinateOnly',
				'x': ('ecdsa-nistp256-with-sha256-X', r),
			},
			's': ('ecdsa-nistp256-with-sha256-s', s)
		}
		encoded_er += struct.pack(">B", 0xa2) + self.asn1.encode('Signature', EnrolmentRequest['signature'])[1:]

		encoded_er = itss.encode_der_SEQUENCE(encoded_er)
	
		# Send request to Enrollment Authority
		r = requests.put(self.EA_url + '/cits/ts_102941_v111/ea/enroll', data=encoded_er)
		
		EnrolmentResponse = self.asn1.decode('EnrolmentResponse', r.content)
		if EnrolmentResponse[0] != 'successfulEnrolment':
			print("Enrollment failed!")
			pprint.pprint(EnrolmentResponse)
			sys.exit(1)

		print("Enrollment finished successfuly.")
		self.EC = itss.CITS103097v121Certificate(EnrolmentResponse[1]['signedCertChain']['rootCertificate'])


	def authorize(self):
		requestTime = int((datetime.datetime.utcnow() - datetime.datetime(2004,1,1)).total_seconds())
		expiration = requestTime +  3600 # 1 hour

		AuthorizationRequest = {

			# The enrolment certificate containing the pseudonymous identifier to be used by the ITS-S
			'signerAuthRequest': {
				'type': 3, # SignerIdType.certificate (fixed)
				'digest': self.EC.Digest,
				'id': b'',
			},

			'authCertRequest' : ('anonRequest', {
				'versionAndType': 2, # explicitCert (fixed)
				'requestTime': requestTime,
				'subjectType': 0, # SecDataExchAnon (fixed)
				'cf': (b'\0',0), # useStartValidity
				'authCertSpecificData': {
					'additional-data': b'ahoj',
					'permissions': { # PsidSspArray
						'type': 1, # ArrayType / specified
						'permissions-list': [] # shall contain a list of the ETSI ITS-AIDs to be supported.
					},
					'region': {
						'region-type': 0 # RegionType / from-issuer
					}
				},
				'responseEncryptionKey': {
					'algorithm': 1, # PKAlgorithm ecdsaNistp256WithSha256
					'public-key': {
						'type': 'compressedLsbY0', # EccPublicKeyType compressedLsbY0
						'x': (
							'ecdsa-nistp256-with-sha256-X',
							0, #response_encryption_public_numbers.x,
						)
					}
				},
			}),
		}

		encoded_ar = b''
		encoded_ar += struct.pack(">B", 0xa0) + self.asn1.encode('SignerIdentifier', AuthorizationRequest['signerAuthRequest'])[1:]
		acr = struct.pack(">B", 0xa0) + self.asn1.encode('AuthCertRequest', AuthorizationRequest['authCertRequest'])[1:]
		encoded_ar += struct.pack(">B", 0xa1) + itss.encode_der_length(len(acr)) + acr

		# Sign with ecdsa_nistp256_with_sha256
		signature_RFC3279 = self.PrivateKey.sign(
			encoded_ar,
			cryptography.hazmat.primitives.asymmetric.ec.ECDSA(
				cryptography.hazmat.primitives.hashes.SHA256()
			)
		)
		r, s = cryptography.hazmat.primitives.asymmetric.utils.decode_dss_signature(signature_RFC3279)

		AuthorizationRequest['signature'] = {
			'r': {
				'type': 'xCoordinateOnly',
				'x': ('ecdsa-nistp256-with-sha256-X', r),
			},
			's': ('ecdsa-nistp256-with-sha256-s', s)
		}

		encoded_ar += struct.pack(">B", 0xa2) + self.asn1.encode('Signature', AuthorizationRequest['signature'])[1:]
		encoded_ar = itss.encode_der_SEQUENCE(encoded_ar)

		# Send request to Authorization Authority
		r = requests.put(self.AA_url + '/cits/ts_102941_v111/aa/approve', data=encoded_ar)

		AuthorizationResponse = self.asn1.decode('AuthorizationResponse', r.content)
		if AuthorizationResponse[0] not in ('successfulExplicitAuthorization', 'successfulImplicitAuthorization'):
			print("Authorization failed!")
			pprint.pprint(AuthorizationResponse)
			sys.exit(1)

		# TODO: Handle also CRL (they can be part of the AuthorizationResponse)

		print("Authorization ticket obtained successfuly.")
		self.AT = itss.CITS103097v121Certificate(AuthorizationResponse[1]['signedCertChain']['rootCertificate'])



	def store(self):
		if self.Engine is not None:
			pass # Don't save anything when on HSM
		else:
			x = self.PrivateKey.private_bytes(
				encoding=cryptography.hazmat.primitives.serialization.Encoding.DER,
				format=cryptography.hazmat.primitives.serialization.PrivateFormat.PKCS8,
				encryption_algorithm=cryptography.hazmat.primitives.serialization.BestAvailableEncryption(b'strong-and-secret :-)')
			)
			open(os.path.join(self.Directory, 'itss.key'),'wb').write(x)

		if self.EC is not None:
			open(os.path.join(self.Directory, 'itss.ec'),'wb').write(self.EC.Data)
		else:
			os.unlink(os.path.join(self.Directory, 'itss.ec'))

		if self.AT is not None:
			open(os.path.join(self.Directory,'itss.at'), 'wb').write(self.AT.Data)
		else:
			os.unlink(os.path.join(self.Directory, 'itss.at'))


	def load(self):
		assert(self.PrivateKey is None)
		assert(self.EC is None)
		assert(self.AT is None)

		if self.Engine is not None:
			p11uri = "pkcs11:object=test-key;type=private"
			backend = cryptography.hazmat.backends.default_backend()
			pkey = backend._lib.ENGINE_load_private_key(
				self.Engine,
				p11uri.encode("utf-8"),
				backend._ffi.NULL,
				backend._ffi.NULL
			)
			backend.openssl_assert(pkey != backend._ffi.NULL)
			pkey = backend._ffi.gc(pkey, backend._lib.EVP_PKEY_free)
			self.P11URI = p11uri
			self.PrivateKey = backend._evp_pkey_to_private_key(pkey)
		else:
			try:
				self.PrivateKey = cryptography.hazmat.primitives.serialization.load_der_private_key(
					open(os.path.join(self.Directory, 'itss.key'),'rb').read(),
					password=b'strong-and-secret :-)',
					backend=cryptography.hazmat.backends.default_backend()
				)
			except:
				return False

		try:
			ecraw = open(os.path.join(self.Directory, 'itss.ec'),'rb').read()
		except FileNotFoundError:
			pass
		else:
			self.EC = itss.CITS103097v121Certificate(ecraw)

		try:
			atraw = open(os.path.join(self.Directory, 'itss.at'),'rb').read()
		except FileNotFoundError:
			pass
		else:
			self.AT = itss.CITS103097v121Certificate(atraw)

		return True


	def get_certificate_by_digest(self, digest):
		'''
		Obtain certificate by its digest.
		Firstly, look at the certificate store in a memory.
		Secondly, look at the certificate store at the local drive.
		Lastly, use AA API to fetch certficate.
		'''
		try:
			return self.Certs[digest]
		except KeyError:
			pass

		cert_fname = os.path.join(self.Directory, "certs", digest.hex() + '.cert')

		try:
			f = open(cert_fname, 'rb')
			data = f.read()
			cert = itss.CITS103097v121Certificate(data)

		except FileNotFoundError:
			r = requests.get(self.AA_url + '/cits/digest/{}'.format(digest.hex()))
			cert = itss.CITS103097v121Certificate(r.content)
			self.store_certificate(cert)

		self.Certs[digest] = cert
		return cert


	def store_certificate(self, certificate):
		cert_fname = os.path.join(self.Directory, "certs", certificate.Digest.hex() + '.cert')
		open(cert_fname, 'wb').write(certificate.Data)


def main():
	parser = argparse.ArgumentParser(
		formatter_class=argparse.RawDescriptionHelpFormatter,
		description=\
'''C-ITS ITS-S reference implementation focused on a security.
C-ITS standards: ETSI TS 102 941 v1.1.1, ETSI TS 103 097 v1.2.1
It also contains a simple ITS-G5 network simulator that utilizes UDP IPv4 multicast.

Copyright (c) 2018 Ales Teska, TeskaLabs Ltd, MIT Licence
''')

	parser.add_argument('DIR', default='.', help='A directory with persistent storage of a keying material')
	parser.add_argument('-e', '--ea-url', default="https://via.teskalabs.com/croads/demo-ca", help='URL of the Enrollment Authority')
	parser.add_argument('-a', '--aa-url', default="https://via.teskalabs.com/croads/demo-ca", help='URL of the Authorization Authority')
	parser.add_argument('--g5-sim', default="224.1.1.1 5007 32 auto", help='Configuration of G5 simulator')

	args = parser.parse_args()

	itss_obj = ITSS(args.DIR, args.ea_url, args.aa_url)
	ok = itss_obj.load()
	store = False
	if not ok:
		itss_obj.generate_private_key()
		store = True

	if itss_obj.EC is None:
		itss_obj.enroll()
		store = True

	if itss_obj.AT is None:
		itss_obj.authorize()
		store = True

	if store:
		itss_obj.store()

	print("ITS-S identity: {}".format(itss_obj.EC.identity()))
	print("AT digest: {}".format(itss_obj.AT.Digest.hex()))


	loop = asyncio.get_event_loop()


	# Create simulator and a handling routine for inbound messages
	class MyG5Simulator(itss.G5Simulator):
		def datagram_received(self, data, addr):
			try:
				msg = itss.CITS103097v121SecureMessage(data)
				signer_certificate = msg.verify(itss_obj)
				print("Received verified message {} from {}".format(msg.Payload, signer_certificate))
			except Exception as e:
				print("Error when processing message")
				traceback.print_exc()

	g5sim = MyG5Simulator(loop, args.g5_sim)


	# Send out some payload periodically
	async def periodic_sender():
		while True:
			smb = itss.CITS103097v121SecureMessageBuilder()
			msg = smb.finish(itss_obj.AT, itss_obj.PrivateKey, "payload")

			g5sim.send(msg)
			await asyncio.sleep(1)	
	asyncio.ensure_future(periodic_sender(), loop=loop)


	print("Ready.")

	try:
		loop.run_forever()
	except KeyboardInterrupt:
		pass

	loop.close()


if __name__ == '__main__':
	main()

