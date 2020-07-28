# Setup:
# virtualenv -p /usr/bin/python3 env
# source env/bin/activate
# pip install -r requirements.txt
# python self-test.py --help

import argparse
from collections import defaultdict
import json
import sys

from bs4 import BeautifulSoup
import requests
from requests.auth import HTTPBasicAuth


parser = argparse.ArgumentParser(
    description='Test the behavior of an OPDS server within the Library Simplified ecosystem'
)
parser.add_argument(
    '--registry-url', help="URL to the library registry"
)
parser.add_argument(
    '--library',
    help='Name of the library to test (as seen in the library registry)'
)
parser.add_argument(
    '--opds-server',
    help="An OPDS server endpoint URL. When specified, `--registry-url` and `--library` flags will be ignored."
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


class Constants(object):

    # Constants for media types
    OPDS_1 = 'application/atom+xml;profile=opds-catalog;kind=acquisition'
    OPDS_2 = 'application/opds+json'
    AUTHENTICATION_DOCUMENT = 'application/vnd.opds.authentication.v1.0+json'
    PATRON_PROFILE_DOCUMENT = "vnd.librarysimplified/user-profile+json"

    ACSM = "application/vnd.adobe.adept+xml"
    OPDS_ENTRY = "application/atom+xml;type=entry;profile=opds-catalog"
    AUDIOBOOK_JSON = "application/audiobook+json"

    PROBLEM_DETAIL = "application/api-problem+json"

class MakesRequests(Constants):

    CONTENT_TYPE = None
    NAME = None

    def __init__(self, url, name=None, auth=None, expect_content_type=None):
        self.url = url
        self.auth = auth
        self.name = name or self.NAME
        self.expect_content_type = expect_content_type or self.CONTENT_TYPE
        self._representation = None

    def get(self):
        if not self._representation:
            response = self.request(self.url, self.name, self.expect_content_type)
            self._representation = response.content
        return self._representation

    def p(self, msg):
        print(msg.encode("utf8"))

    def error(self, error):
        self.p("ERROR: %s" % error)

    def warn(self, warning):
        self.p("WARN: %s" % warning)

    def request(self, url, name, expect_content_type):
        response = requests.get(url, auth=self.auth)
        self.p("Retrieved %s from %s" % (name, url))

        if response.status_code / 100 != 2:
            self.warn("Status code was %s." % response.status_code)

        content_type = response.headers.get('Content-Type')
        if content_type == self.PROBLEM_DETAIL:
            self.warn(
                "Got a problem detail document: %r" % response.content
            )
        if expect_content_type and (not content_type or not content_type.startswith(expect_content_type)):
            self.warn(
                "Expected content type %s, got %s" % (
                    expect_content_type, content_type
                )
            )
        self.p(" %d bytes, %s" % (len(response.content), content_type))
        if args.verbose:
            self.p("-" * 80)
            content = response.content.decode("utf8")
            if 'xml' in content_type:
                content = BeautifulSoup(content, 'xml').prettify()
            elif 'json' in content_type:
                content = json.dumps(json.loads(content), sort_keys=True, indent=4)
        
            self.p(content)
            self.p("-" * 80)
        return response

class Fulfillment(MakesRequests):

    REGISTRY = {}

    @classmethod
    def fulfill(cls, url, name, type, auth):
        fulfillment_class = cls.REGISTRY.get(type, Fulfillment)
        fulfillment = fulfillment_class(url, name, auth, expect_content_type=type)
        fulfillment.validate()

    def validate(self):
        # Generic implementation
        self.get()

    @classmethod
    def register(cls, subclass):
        cls.REGISTRY[subclass.MEDIA_TYPE] = subclass

class ACSMFulfillment(Fulfillment):

    MEDIA_TYPE = Constants.ACSM

    def validate(self):
        result = self.get()
        parsed = BeautifulSoup(result, "xml")
        token = parsed.find('fulfillmentToken')
        if token:
            self.p(
                "Found fulfillmentToken tag -- this looks like a real ACSM file."
            )
        else:
            self.warn(
                "No fulfillmentToken tag -- this might not be a real ACSM file."
            )
Fulfillment.register(ACSMFulfillment)

class AudiobookJSONFulfillment(Fulfillment):
    MEDIA_TYPE = Constants.AUDIOBOOK_JSON

    def validate(self):
        result = self.get()
        parsed = json.loads(result)
        if not 'readingOrder' in parsed:
            self.error("readingOrder not present in audiobook manifest")
        order = parsed['readingOrder']
        if not order:
            self.error("No items in reading order.")
        else:
            self.p("Items in reading order: %s" % len(order))
            item1 = order[0]
            self.p("Trying to fulfill first item.")
            type = item1.get('type', None)

            # Make a recursive call to Fulfillment.fulfill
            # NOTE: for now we are not passing along self.auth
            #  because the recursive call might go outside the CM.
            Fulfillment.fulfill(
                item1['href'], "first audiobook item", type, auth=None
            )
Fulfillment.register(AudiobookJSONFulfillment)

class PatronProfileDocument(MakesRequests):

    NAME = "patron profile document"
    MEDIA_TYPE = Constants.PATRON_PROFILE_DOCUMENT

    def validate(self):
        data = json.loads(self.get())
        adobe_credentials = False
        if 'drm' in data:
            for drm in data['drm']:
                vendor = drm.get('drm:vendor')
                scheme = drm.get('drm:scheme')
                token = drm.get('drm:clientToken')
                if scheme != 'http://librarysimplified.org/terms/drm/scheme/ACS':
                    self.warn("Unknown DRM scheme seen: %s" % scheme)
                    continue
                if vendor and token:
                    adobe_credentials = (vendor, token)
                    break
        if adobe_credentials:
            self.p("Adobe token found: %s, %s" % (vendor, token))
        else:
            self.warn("No Adobe token found.")

class AuthenticationDocument(MakesRequests):

    NAME = "authentication document"
    MEDIA_TYPE = Constants.AUTHENTICATION_DOCUMENT
    
    def __init__(self, url):
        super(AuthenticationDocument, self).__init__(url, None)
        self.data = json.loads(self.get())

    def set_auth(self, username, password):
        self.auth = HTTPBasicAuth(username, password)

    @property
    def main_catalog(self):
        links = [
            x['href'] for x in self.data['links']
            if x.get('rel') == 'start'
            and x.get('type', '').startswith(self.OPDS_1)
        ]
        if not links:
            self.error(
                "Authentication document does not contain a usable 'start' link!"
            )
        return OPDS1Feed(links[0], "main catalog", self.auth)

    def link_with_rel(self, rel):
        links = [
            x['href'] for x in self.data['links']
            if x.get('rel') == rel
        ]
        if not links:
            self.error(
                'Authentication document has no link with rel="%s"!' % rel
            )
        if links:
            return links[0]
        return None

    @property
    def patron_profile_document(self):
        url = self.link_with_rel(
            "http://librarysimplified.org/terms/rel/user-profile"
        )
        if not url:
            return None
        return PatronProfileDocument(url, auth=self.auth)

    @property
    def bookshelf(self):
        url = self.link_with_rel("http://opds-spec.org/shelf")
        if not url:
            return None
        return Bookshelf(url, "bookshelf", self.auth)

class OPDS1Feed(MakesRequests):
    def get(self):
        super(OPDS1Feed, self).get()
        self._representation = BeautifulSoup(self._representation, "lxml")
        return self._representation

    @property
    def entries(self):
        for e in self.get().find_all('entry'):
            yield e

    def validate(self):
        collections = defaultdict(list)
        titles = []
        for e in self.entries:
            title = e.find('title')
            if title:
                title = title.string
            else:
                title = None
            titles.append(title)
            collection = e.find('link', rel="collection")
            if collection:
                collection_title = collection.get('title', None)
                collections[collection_title].append(title)
        if collections:
            self.p("This is a grouped feed:")
            for k, v in sorted(collections.items()):
                self.p(" %s: %d titles" % (k, len(v)))
        else:
            self.p(
                "This is an ungrouped feed containing %d titles." % len(titles)
            )

class Bookshelf(OPDS1Feed):

    def validate(self):
        for entry in self.entries:
            self.validate_entry(entry)

    def validate_entry(self, entry):
        fulfillment_links = entry.find_all(
            'link', rel="http://opds-spec.org/acquisition",
        )
        title = entry.find('title').string
        if not fulfillment_links:
            self.warn(
                "No fulfillment links found for patron; cannot test fulfillment."
            )

        for link in fulfillment_links:
            type = link['type']
            name = 'fulfillment of "%s" (supposedly as %s)' % (title, type)
            Fulfillment.fulfill(link['href'], name, type, self.auth)


class LibraryRegistry(MakesRequests):

    NAME = "library registry"
    MEDIA_TYPE = Constants.OPDS_2

    def __init__(self, url):
        super(LibraryRegistry, self).__init__(url)
        self.library_list = self.get()
        libraries = json.loads(self.library_list)
        self.libraries = {}
        for l in libraries['catalogs']:
            self.libraries[l['metadata']['title']] = l

    def authentication_document(self, name):
        if name not in self.libraries:
            return None
        authentication_link = None
        for link in self.libraries[name]['links']:
            if link['type'] == self.AUTHENTICATION_DOCUMENT:
                authentication_link = link['href']
                break
        if not authentication_link:
            self.error(
                "No authentication link found for library %s" % name
            )
        return AuthenticationDocument(authentication_link)


def main():
    DEFAULT_REGISTRY_URL = "https://libraryregistry.librarysimplified.org/libraries/qa"

    # If we're given a library's OPDS server endpoint, we'll use that to get
    # the authentication document and will ignore the `--registry-url` and
    # `--library` flags. Otherwise, we'll use `--registry-url` and `--library`
    # to find the authentication document.
    if args.opds_server is not None:
        if args.registry_url or args.library:
            print("WARNING: `--opds-server` specified. Ignoring `--registry-url` and `--library` flags.")
        opds_server = args.opds_server + '/' if not args.opds_server.endswith('/') else ''
        authentication_document = AuthenticationDocument(opds_server + "authentication_document")
    else:
        # We start by connecting to the library registry and locating the
        # requested library.
        registry_url = args.registry_url or DEFAULT_REGISTRY_URL
        registry = LibraryRegistry(registry_url)

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


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nReceived keyboard interrupt. Ending.')
