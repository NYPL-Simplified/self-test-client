# Setup:
# virtualenv -p /usr/bin/python3 env
# source env/bin/activate
# pip install -r requirements.txt
# python self-test.py --help

import click
from collections import defaultdict
import json
import re
import sys
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import requests
from requests.auth import HTTPBasicAuth

class Constants(object):

    verbose = False

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
        click.echo(msg)

    def error(self, error):
        click.echo(click.style("ERROR ", fg="red", bold=True), nl=False)
        click.echo(error)

    def warn(self, warning):
        click.echo(click.style("WARN ", fg="red", bold=False), nl=False)
        click.echo(warning)

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
        if self.verbose:
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

    def validate(self, registry):
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
            registry.validate_short_client_token(token)
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


class InvalidShortClientTokenException(Exception):
    ...

class LibraryRegistry(MakesRequests):

    NAME = "library registry"
    MEDIA_TYPE = Constants.OPDS_2

    CLIENT_TOKEN_RE = re.compile(r'''
                                ^
                                (?P<library>[^|]+) \|               # Any number of characters up to a pipe
                                (?P<timestamp>[0-9]+) \|            # Epoch timestamp, any number of digits
                                (?P<patron_id>[-A-Za-z0-9]{36}) \|  # The patron id is a UUID, 36 characters long
                                (?P<signature_hash>.*)              # Everything after the third pipe
                                $
                             ''', re.IGNORECASE | re.VERBOSE)
    REGISTRY_RESPONSE_RE = re.compile(r'''<user>(?P<user_id>[^<]+?)</user>''', re.IGNORECASE)

    def __init__(self, url):
        super(LibraryRegistry, self).__init__(url)
        self.library_list = self.get()
        libraries = json.loads(self.library_list)
        self.libraries = {}
        for l in libraries['catalogs']:
            self.libraries[l['metadata']['title']] = l

    def authentication_document(self, name):
        if name.startswith("http"):
            # This is probably the URL to the authentication document.
            # Just return it; this allows us to test libraries not in the
            # registry.
            return AuthenticationDocument(name)
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

    def validate_short_client_token(self, token):
        """
        Retrieve an Adobe ID from a Short Client Token and validate it.

        A short token is a four part, pipe-separated string which follows this pattern:

        \b
        <library-code>|<epoch-timestamp>|<patron-id>|<signature-hash>

        Example:

        \b
        NYNYPL|1621462513|3e0d6602-2446-4f1a-bcad-4e68bcffdfc1|xzu4JDv93sjAEzx1sSIxyWrXn;zXD62;vsR:LT1y8M0@

        :param token: A string
        :raise InvalidShortClientTokenException: If the token is invalid.
        """
        (library, timestamp, patron_id, signature_hash) = self.decompose_token(token)

        click.echo("\nThe supplied Short Client Token was well formed, and decomposes to:\n")
        click.echo(f"  Library code:      {library}")
        click.echo(f"  Timestamp:         {timestamp}")
        click.echo(f"  Patron identifier: {patron_id}")
        click.echo(f"  Signature:         {signature_hash}\n")

        signin_url = urljoin(self.url, "/AdobeAuth/SignIn")

        username = "|".join([library, timestamp, patron_id])

        signin_payload_lines = [
            '<signInRequest method="standard" xmlns="http://ns.adobe.com/adept">',
            f"    <username>{username}</username>",
            f"    <password>{signature_hash}</password>",
            "</signInRequest>",
        ]

        click.echo(f"\nRequesting {signin_url}\n")
        if self.verbose:
            for line in signin_payload_lines:
                click.echo(f"    {line}")

        response = requests.post(signin_url, data="".join(signin_payload_lines))

        if self.verbose:
            click.echo("\nRegistry server responded with:\n")
            for line in response.content.decode('utf8').split("\n"):
                click.echo(f"    {line}")
            click.echo()

        user_id_match = self.REGISTRY_RESPONSE_RE.search(response.content.decode('utf8'))
        if user_id_match:
            click.echo(click.style("SUCCESS ", fg="green", bold=True), nl=False)
            click.echo(f"Adobe ID for this patron is {user_id_match.group('user_id')}")
            click.echo(click.style(" SUCCESS", fg="green", bold=True))
        else:
            click.echo(click.style("ERROR ", fg="red", bold=True), nl=False)
            click.echo("Supplied token could not be turned into an Adobe ID", nl=False)
            click.echo(click.style(" ERROR", fg="red", bold=True))

    def decompose_token(self, token):
        if "drm:clientToken" in token:
            token = token.replace("<drm:clientToken>", "").replace("</drm:clientToken>", "")
        m = self.CLIENT_TOKEN_RE.match(token)

        if not m:
            raise InvalidShortClientTokenException(f"Invalid token: {token}")
        else:
            return m.groups()   # Tuple of library, timestamp, patron_id, signature_hash

@click.command()
@click.option(
    '--registry-url', help="URL to the library registry",
    metavar="<URL>",
    default = "https://libraryregistry.librarysimplified.org/libraries/qa"
)
@click.option(
    '--library',
    help='Name of the library to test (as seen in the library registry), or the URL to its authentication document'
)
@click.option(
    '--username', help="Username to present to the OPDS server."
)
@click.option(
    '--password', help="Password to present to the OPDS server.",
    default=""
)
@click.option(
    '--verbose', help='Produce verbose output',
    is_flag=True, default=False
)
def main(registry_url, library, username, password, verbose):

    Constants.verbose = verbose

    # We start by connecting to the library registry and locating the
    # requested library.
    registry = LibraryRegistry(registry_url)

    # We then fetch that library's authentication document.
    authentication_document = registry.authentication_document(library)
    if not authentication_document:
        click.echo(f"Library not found: {library}")
        click.echo("Available libraries:")
        for i in sorted(registry.libraries.keys()):
            click.echo(f" {i}")
        sys.exit()

    # At this point we need to start making authenticated requests.
    authentication_document.set_auth(username, password)

    # The authentication document links to the OPDS server's main catalog
    # and to the patron profile document
    patron_profile_document = authentication_document.patron_profile_document
    if patron_profile_document:
        patron_profile_document.validate(registry)

    # It also links to the patron's bookshelf.
    bookshelf = authentication_document.bookshelf
    if bookshelf:
        bookshelf.validate()

    # And it links to the main catalog.
    main_catalog = authentication_document.main_catalog
    if main_catalog:
        main_catalog.validate()

main()
