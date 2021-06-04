# Setup:
# virtualenv -p /usr/bin/python3 env
# source env/bin/activate
# pip install -r requirements.txt
# python self-test.py --help

import argparse
import sys
import model
from model import *

parser = argparse.ArgumentParser(
    description='Test the behavior of an OPDS server within the Library Simplified ecosystem'
)
parser.add_argument(
    '--registry-url', help="URL to the library registry",
    default = "https://libraryregistry.librarysimplified.org/libraries/qa"
)
parser.add_argument(
    '--library',
    help='Name of the library to test (as seen in the library registry)'
)
parser.add_argument(
    '--username', help="Username to present to the OPDS server."
)
parser.add_argument(
    '--password', help="Password to present to the OPDS server.",
    default=""
)
parser.add_argument(
    '--verbose', help='Produce verbose output',
    action="store_true", default=False
)
args = parser.parse_args()
model.verbose = args.verbose

# We start by connecting to the library registry and locating the
# requested library.
registry = LibraryRegistry(args.registry_url)

# We then fetch that library's authentication document.
authentication_document = registry.authentication_document(args.library)
if not authentication_document:
    print("Library not found: %s" % args.library)
    print("Available libraries:")
    for i in sorted(registry.libraries.keys()):
        print((" " + i).encode("utf8"))
    sys.exit()

# At this point we need to start making authenticated requests.
authentication_document.set_auth(args.username, args.password)

# The authentication document links to the OPDS server's main catalog
# and to the patron profile document
patron_profile_document = authentication_document.patron_profile_document
if patron_profile_document:
    patron_profile_document.validate()

# It also links to the patron's bookshelf.
bookshelf = authentication_document.bookshelf
if bookshelf:
    bookshelf.validate()

# And it links to the main catalog.
main_catalog = authentication_document.main_catalog
if main_catalog:
    main_catalog.validate()


