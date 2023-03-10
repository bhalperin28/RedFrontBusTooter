import boto3
import datetime
import os
import re
import requests

from base64 import b64decode
from datetime import datetime, timedelta, timezone
from mastodon import Mastodon

VEHICLE_ID = '8628'
MAP_FILENAME = '/tmp/map.png'
S3_BUCKET_NAME = 'redfrontbusmaps'
VEHICLE_MONITORING_ENDPOINT = 'https://api.511.org/transit/VehicleMonitoring?format=json&agency=SF&vehicleID={}&api_key={}'
NOT_OPERATING_MSG = 'Red Front Bus is not currently operating 😢'

# Use Title Case but make sure ordinals are lowercase (e.g. 19th)
def muniCase(lineName):
	return re.sub(r'([0-9])([A-Z])', lambda m: m.group(1) + m.group(2).lower(), lineName.title())	

def event_handler(event, context):
	sf511ApiKey = boto3.client('kms').decrypt(
	    CiphertextBlob=b64decode(os.environ['SF_511_API_KEY']),
	    EncryptionContext={'LambdaFunctionName': os.environ['AWS_LAMBDA_FUNCTION_NAME']}
	)['Plaintext'].decode('utf-8')
	
	url = VEHICLE_MONITORING_ENDPOINT.format(VEHICLE_ID, sf511ApiKey)
	r = requests.get(url)
	r.encoding='utf-8-sig'
	
	delivery = r.json()['Siri']['ServiceDelivery']['VehicleMonitoringDelivery']
	if 'VehicleActivity' not in delivery:
		print(NOT_OPERATING_MSG)
		return
	journey = delivery['VehicleActivity'][0]['MonitoredVehicleJourney']
	lineRef = journey['LineRef']
	if lineRef is None:
		print(NOT_OPERATING_MSG)
		return
	lineName = muniCase(journey['PublishedLineName'])

	mastodonClientSecret = boto3.client('kms').decrypt(
	    CiphertextBlob=b64decode(os.environ['MASTODON_CLIENT_SECRET']),
	    EncryptionContext={'LambdaFunctionName': os.environ['AWS_LAMBDA_FUNCTION_NAME']}
	)['Plaintext'].decode('utf-8')
	
	mastodonPassword = boto3.client('kms').decrypt(
	    CiphertextBlob=b64decode(os.environ['MASTODON_PASSWORD']),
	    EncryptionContext={'LambdaFunctionName': os.environ['AWS_LAMBDA_FUNCTION_NAME']}
	)['Plaintext'].decode('utf-8')
	
	mastodon = Mastodon(
		client_id = os.environ['MASTODON_CLIENT_ID'],
		client_secret = mastodonClientSecret,
		api_base_url = os.environ['MASTODON_SERVER']
	)
	mastodon.log_in(os.environ['MASTODON_EMAIL'], mastodonPassword)
	
	mastodonId = mastodon.me()['id']
	statuses = mastodon.account_statuses(id=mastodonId, exclude_replies=True, exclude_reblogs=True, limit=1)
	lastPostContent = statuses[0]['content']
	match = re.match('.*operating on route (\S+)', lastPostContent)
	if not match:
		raise Exception('Regex match failed. Exiting')
	lastLineRef = match.groups()[0]
	
	lastPostCreatedAt = statuses[0]['created_at']
	if lineRef == lastLineRef and datetime.now(timezone.utc) - lastPostCreatedAt < timedelta(days=1):
		print('No change in route. Exiting')
		return
	
	s3 = boto3.resource('s3')
	mapImageObject = s3.Object(S3_BUCKET_NAME, lineRef + '.png')
	mapImageObject.download_file(MAP_FILENAME)
	mediaPostDict = mastodon.media_post(media_file=MAP_FILENAME, mime_type='image/png', description='A map of the {} {} route'.format(lineRef, lineName))
	
	msg = '#RedFrontBus 🔴 (#{}) is currently operating on route {} {}. Tag me if you see me!'.format(VEHICLE_ID, lineRef, lineName)
	print(msg)
	mastodon.status_post(status=msg, media_ids=[mediaPostDict.id])
	
