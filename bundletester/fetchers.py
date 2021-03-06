import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile

import requests
import yaml

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECS = 45


def get(*args, **kw):
    if 'timeout' not in kw:
        kw['timeout'] = REQUEST_TIMEOUT_SECS

    return requests.get(*args, **kw)


def is_int(string):
    try:
        int(string)
        return True
    except ValueError:
        return False


def rename(dir_):
    """If ``dir_`` is a charm directory, rename it to match the
    charm name, otherwise do nothing.

    :param dir_: directory path
    :return: the new directory name (possibly unchanged).

    """
    dir_ = dir_.rstrip(os.sep)
    metadata = os.path.join(dir_, "metadata.yaml")
    if not os.path.exists(metadata):
        return dir_
    metadata = yaml.safe_load(open(metadata))
    name = metadata.get("name")
    if not name:
        return dir_
    new_dir = os.path.join(os.path.dirname(dir_), name)
    os.rename(dir_, new_dir)
    return new_dir


def extract_archive(archive, dir_):
    """Extract zip archive at filesystem path ``archive`` into directory
    ``dir_`` and return the full path to the directory containing the
    extracted archive.

    """
    tempdir = tempfile.mkdtemp(dir=dir_)
    log.debug("Extracting %s to %s", archive, tempdir)
    # Can't  extract with python due to bug that drops file
    # permissions: http://bugs.python.org/issue15795
    # In particular, it's important that executable test files in the
    # archive remain executable, otherwise the tests won't be run.
    # Instead we use a shell equivalent of the following:
    #     archive = zipfile.ZipFile(archive, 'r')
    #     archive.extractall(tempdir)
    check_call('unzip {} -d {}'.format(archive, tempdir))
    return tempdir


def download_file(url, dir_):
    """Download file at ``url`` into directory ``dir_`` and return the full
    path to the downloaded file.

    """
    _, filename = tempfile.mkstemp(dir=dir_)
    log.debug("Downloading %s", url)
    r = get(url, stream=True)
    with open(filename, 'wb') as f:
        for chunk in r.iter_content(chunk_size=1024):
            if chunk:  # filter out keep-alive new chunks
                f.write(chunk)
                f.flush()
    return filename


class Fetcher(object):
    def __init__(self, url, **kw):
        self.revision = ''
        self.url = url
        for k, v in kw.items():
            setattr(self, k, v)

    @classmethod
    def can_fetch(cls, url):
        match = cls.MATCH.search(url)
        return match.groupdict() if match else {}

    def get_revision(self, dir_):
        dirlist = os.listdir(dir_)
        if '.bzr' in dirlist:
            rev_info = check_output('bzr revision-info', cwd=dir_)
            return rev_info.split()[1]
        elif '.git' in dirlist:
            return check_output('git rev-parse HEAD', cwd=dir_)
        elif '.hg' in dirlist:
            return check_output(
                "hg log -l 1 --template '{node}\n' -r .", cwd=dir_)
        else:
            return self.revision


class BzrFetcher(Fetcher):
    MATCH = re.compile(r"""
    ^(lp:|launchpad:|https?://((code|www)\.)?launchpad.net/)
    (?P<repo>[^@]*)(@(?P<revision>.*))?$
    """, re.VERBOSE)

    @classmethod
    def can_fetch(cls, url):
        matchdict = super(BzrFetcher, cls).can_fetch(url)
        return matchdict if '/+merge/' not in matchdict.get('repo', '') else {}

    def fetch(self, dir_):
        dir_ = tempfile.mkdtemp(dir=dir_)
        url = 'lp:' + self.repo
        cmd = 'branch --use-existing-dir {} {}'.format(url, dir_)
        if self.revision:
            cmd = '{} -r {}'.format(cmd, self.revision)
        bzr(cmd)
        return rename(dir_)


class BzrMergeProposalFetcher(BzrFetcher):
    @classmethod
    def can_fetch(cls, url):
        matchdict = super(BzrFetcher, cls).can_fetch(url)
        return matchdict if '/+merge/' in matchdict.get('repo', '') else {}

    def fetch(self, dir_):
        dir_ = tempfile.mkdtemp(dir=dir_)
        api_base = 'https://api.launchpad.net/devel/'
        url = api_base + self.repo
        merge_data = get(url).json()
        target = 'lp:' + merge_data['target_branch_link'][len(api_base):]
        source = 'lp:' + merge_data['source_branch_link'][len(api_base):]
        bzr('branch --use-existing-dir {} {}'.format(target, dir_))
        bzr('merge {}'.format(source), cwd=dir_)
        bzr('commit --unchanged -m "Merge commit"', cwd=dir_)
        return rename(dir_)


class GithubFetcher(Fetcher):
    MATCH = re.compile(r"""
    ^(gh:|github:|https?://(www\.)?github.com/)
    (?P<repo>[^@]*)(@(?P<revision>.*))?$
    """, re.VERBOSE)

    def fetch(self, dir_):
        dir_ = tempfile.mkdtemp(dir=dir_)
        url = 'https://github.com/' + self.repo
        git('clone {} {}'.format(url, dir_))
        if self.revision:
            git('checkout {}'.format(self.revision), cwd=dir_)
        return rename(dir_)


class BitbucketFetcher(Fetcher):
    MATCH = re.compile(r"""
    ^(bb:|bitbucket:|https?://(www\.)?bitbucket.org/)
    (?P<repo>[^@]*)(@(?P<revision>.*))?$
    """, re.VERBOSE)

    def fetch(self, dir_):
        dir_ = tempfile.mkdtemp(dir=dir_)
        url = 'https://bitbucket.org/' + self.repo
        if url.endswith('.git'):
            return self._fetch_git(url, dir_)
        return self._fetch_hg(url, dir_)

    def _fetch_git(self, url, dir_):
        git('clone {} {}'.format(url, dir_))
        if self.revision:
            git('checkout {}'.format(self.revision), cwd=dir_)
        return rename(dir_)

    def _fetch_hg(self, url, dir_):
        cmd = 'clone {} {}'.format(url, dir_)
        if self.revision:
            cmd = '{} -u {}'.format(cmd, self.revision)
        hg(cmd)
        return rename(dir_)


class LocalFetcher(Fetcher):
    @classmethod
    def can_fetch(cls, url):
        src = os.path.abspath(
            os.path.join(os.getcwd(), os.path.expanduser(url)))
        if os.path.exists(src):
            return dict(path=src)
        return {}

    def fetch(self, dir_):
        dst = os.path.join(dir_, os.path.basename(self.path.rstrip(os.sep)))
        shutil.copytree(self.path, dst, symlinks=True)
        return dst


class CharmstoreDownloader(Fetcher):
    MATCH = re.compile(r"""
    ^cs:(?P<entity>.*)$
    """, re.VERBOSE)

    STORE_URL = 'https://api.jujucharms.com/charmstore/v4/{}'
    ARCHIVE_URL = STORE_URL + '/archive'
    REVISION_URL = STORE_URL + '/meta/id-revision'

    def __init__(self, *args, **kw):
        super(CharmstoreDownloader, self).__init__(*args, **kw)

    def fetch(self, dir_):
        url = self.ARCHIVE_URL.format(self.entity)
        archive = download_file(url, dir_)
        entity_dir = extract_archive(archive, dir_)
        return rename(entity_dir)

    def get_revision(self, dir_):
        url = self.REVISION_URL.format(self.entity)
        return get(url).json()['Revision']


class BundleDownloader(CharmstoreDownloader):
    MATCH = re.compile(r"""
    ^bundle:(?P<entity>.*)$
    """, re.VERBOSE)

    def __init__(self, *args, **kw):
        super(BundleDownloader, self).__init__(*args, **kw)
        self.entity = normalize_bundle_name(self.entity)


def normalize_bundle_name(bundle_name):
    """Convert old-style bundle name to new format.

    Example:
        ~charmers/mediawiki/6/single -> ~charmers/mediawiki-single-6

    (for more examples see tests)

    """
    owner, bundle = None, bundle_name
    if bundle.startswith('~'):
        owner, bundle = bundle.split('/', 1)
    bundle_parts = bundle.split('/')
    if len(bundle_parts) == 3 and is_int(bundle_parts[1]):
        bundle_parts = [
            bundle_parts[0],
            bundle_parts[2],
            bundle_parts[1]]
    bundle = '-'.join(bundle_parts)
    if owner:
        bundle = '/'.join((owner, bundle))
    return bundle


def bzr(cmd, **kw):
    check_call('bzr ' + cmd, **kw)


def git(cmd, **kw):
    check_call('git ' + cmd, **kw)


def hg(cmd, **kw):
    check_call('hg ' + cmd, **kw)


def check_call(cmd, **kw):
    return check_output(cmd, **kw)


class FetchError(Exception):
    pass


def check_output(cmd, **kw):
    args = shlex.split(cmd)
    p = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **kw
    )
    out, _ = p.communicate()
    if p.returncode != 0:
        raise FetchError(out)
    log.debug('%s: %s', cmd, out)
    return out


FETCHERS = [
    BzrFetcher,
    BzrMergeProposalFetcher,
    GithubFetcher,
    BitbucketFetcher,
    LocalFetcher,
    CharmstoreDownloader,
    BundleDownloader,
]


def get_fetcher(url):
    for fetcher in FETCHERS:
        matchdict = fetcher.can_fetch(url)
        if matchdict:
            return fetcher(url, **matchdict)
    raise FetchError('No fetcher for url: %s' % url)
