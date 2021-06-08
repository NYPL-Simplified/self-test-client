# Setup:
# virtualenv -p /usr/bin/python3 env
# source env/bin/activate
# pip install -r requirements.txt
# python clever-authentication.py --help

import argparse
import requests
import sys
import model
from model import (
    AuthenticationDocument,
    Constants,
)

parser = argparse.ArgumentParser(
    description='Test the process of authentication with a circulation manager through Clever.'
)
parser.add_argument(
    '--authentication-document-url',
    help="URL to the library's authentication document",
    default = "https://circulation.openebooks.us/USOEI/authentication_document"
)
parser.add_argument(
    '--verbose', help='Produce verbose output',
    action="store_true", default=False
)
args = parser.parse_args()
model.verbose = args.verbose
auth_document_url = args.authentication_document_url
auth = AuthenticationDocument(auth_document_url)
[mechanism] = auth.authentication_mechanisms(Constants.OAUTH_WITH_INTERMEDIARY)
authentication_url = mechanism.link_with_rel('authenticate')
response = requests.get(authentication_url, allow_redirects=False)
web_location = response.headers['Location']

print("Open up this URL in a web browser and log in:")
print(web_location)

