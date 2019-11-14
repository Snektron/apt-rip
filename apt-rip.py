#!/usr/bin/env python3
import argparse
import os
import json
import urllib.request
import zlib
import re
import tempfile
import subprocess
import shutil
from io import BytesIO

DEFAULT_CONFIG = os.path.join(os.path.expanduser('~'), '.config/apt-rip/apt-rip.json')
DEFAULT_DIST = 'eoan'
DEFAULT_REPO = 'main'

APT_RIP_ROOT = 'etc/apt-rip'
DL_BUFFER_SIZE = 8196

PROGRESS_BAR_SIZE = 42

class ProgressBar:
    def __init__(self, msg, total):
        self.total = total
        self.progress = 0

        terminal_width = os.get_terminal_size()[0]
        text_size = terminal_width - 1 - PROGRESS_BAR_SIZE

        if len(msg) > text_size:
            self.msg = '…' + msg[-text_size + 1:] + ' '
        else:
            self.msg = msg + ' ' * (text_size - len(msg) + 1)

    def print(self, first = False):
        if not first:
            print('\x1bM\x1b[2K', end = '')

        if self.total is not None:
            bar_size = (self.progress * (PROGRESS_BAR_SIZE - 2) // self.total)
            print(self.msg + '[' + '=' * bar_size + ' ' * (PROGRESS_BAR_SIZE - 2 - bar_size) + ']')
        else:
            print(self.msg + '[' + '?' * (PROGRESS_BAR_SIZE - 2) + ']')

class Installer:
    def __init__(self, args, config, index, installed, tmp_dir):
        self.args = args
        self.config = config
        self.index = index
        self.installed = installed
        self.new_installed = {}
        self.install_dir = os.path.join(tmp_dir, 'install')
        self.extract_dir = os.path.join(tmp_dir, 'extract')

    def install(self, package, explicit):
        if package in self.installed or package in self.new_installed:
            if explicit:
                print('Package "%s" is already installed' % package)
            return

        dl_url = '%s/%s' % (self.config['mirror'], self.index[package]['filename'])
        data = download(dl_url, show_progress = True)
        deb = os.path.join(self.extract_dir, 'package.deb')
        write_file(deb, data)

        result = subprocess.run(['ar', 'x', deb, 'data.tar.xz'], cwd = self.extract_dir, check = True, stderr = subprocess.PIPE)
        if result.stderr != b'':
            raise Exception('Failed to extract deb')

        subprocess.run(['tar', 'xf', 'data.tar.xz'], cwd = self.extract_dir, check = True, stderr = subprocess.PIPE)
        os.remove(deb)
        os.remove(os.path.join(self.extract_dir, 'data.tar.xz'))

        package_files = []
        for root, dirs, files in os.walk(self.extract_dir):
            for file in files:
                src_path = os.path.join(root, file)
                rel_path = os.path.relpath(src_path, self.extract_dir)
                package_files.append(rel_path)

                root_path = os.path.join(self.config['install_root'], rel_path)
                install_path = os.path.join(self.install_dir, rel_path)

                if os.path.exists(root_path):
                    conflicting = root_path
                elif os.path.exists(install_path):
                    conflicting = install_path
                else:
                    conflicting = None

                if conflicting is not None:
                    raise Exception('File "%s" of package "%s" conflicts with existing file' % (conflicting, package))
                move_file(src_path, install_path)

        dependencies = self.index[package]['depends'] if 'depends' in self.index[package] else []

        install_info = {
            'dist': self.args.dist,
            'repo': self.args.repo,
            'explicit': explicit,
            'files': package_files,
            'depends': [dep.split(' ')[0] for dep in dependencies]
        }

        self.new_installed[package] = install_info

        for dep in dependencies:
            dep_pkg = dep.split(' ')[0] # Ignore version
            self.install(dep_pkg, False)

def move_file(src, dst):
    if not os.path.exists(os.path.dirname(dst)):
        os.makedirs(os.path.dirname(dst))
    shutil.move(src, dst)

def write_file(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.exists(directory):
        os.makedirs(directory)

    with open(path, 'wb') as f:
        f.write(data)

def read_file(path):
    with open(path, 'rb') as f:
        return f.read()

def read_config(path):
    if os.path.exists(path):
        return json.loads(read_file(path))

    default_config = {
        'mirror': 'https://mirrors.edge.kernel.org/ubuntu',
        'install_root': os.path.join(os.path.expanduser('~'), '.local')
    }

    write_file(path, json.dumps(default_config, indent = 4).encode())

    return default_config

def read_installed(config):
    path = os.path.join(config['install_root'], APT_RIP_ROOT, 'installed_packages.json')
    if os.path.exists(path):
        return json.loads(read_file(path))
    return {}

def write_installed(config, installed):
    path = os.path.join(config['install_root'], APT_RIP_ROOT, 'installed_packages.json')
    write_file(path, json.dumps(installed, indent = 4).encode())

def download(url, *, show_progress = False):
    response = urllib.request.urlopen(url)
    data = bytearray()

    if show_progress:
        cl = response.getheader('Content-Length')

        bar = ProgressBar(url, int(cl) if cl is not None else None)
        bar.print(True)

    while True:
        buf = response.read(DL_BUFFER_SIZE)
        if not buf:
            break

        if show_progress:
            bar.progress += len(buf)
            bar.print()

        data += buf
    return data

def read_package_index(config, dist, repo):
    path = os.path.join(config['install_root'], APT_RIP_ROOT, 'package-indices', '%s-%s.json' % (dist, repo))
    if os.path.exists(path):
        return json.loads(read_file(path))

    url = '%s/dists/%s/%s/binary-amd64/Packages.gz' % (config['mirror'], dist, repo)
    data = download(url, show_progress = True)
    packages_info = zlib.decompress(data, zlib.MAX_WBITS + 16).decode('utf-8')
    packages = {}

    for package_info in packages_info.split('\n\n'):
        package = {}
        if package_info == "":
            break

        for line in package_info.split('\n'):
            field, value = line.split(': ', 1)
            if field in ['Version', 'Filename']:
                package[field.lower()] = value
            elif field == 'Package':
                name = value
            elif field == 'Depends':
                package['depends'] = value.split(', ')

        if name in packages:
            raise Exception('Error: Duplicate package "%s" in index' % name)

        packages[name] = package
    write_file(path, json.dumps(packages, indent = 4).encode())
    return packages

def find_packages(index, query):
    return [name for name in index if query in name]

def direct_reverse_dependencies(installed, package):
    for rdep, info in installed.items():
        if package in info['depends']:
            yield rdep

def remove(config, installed, package, quiet):
    if not package in installed:
        return

    rdeps = list(direct_reverse_dependencies(installed, package))
    if rdeps:
        if quiet:
            return
        raise Exception('Package "%s" is required by %d other packages' % (package, len(rdeps)))

    for file in installed[package]['files']:
        path = os.path.join(config['install_root'], file)

        if not os.path.exists(path):
            print('Warning: File "%s" of package "%s" appears to be missing' % (path, package))
        else:
            os.remove(path)

        try:
            os.removedirs(os.path.dirname(path))
        except:
            pass

    deps = installed[package]['depends']
    del installed[package]
    for dep in deps:
        if dep in installed and not installed[dep]['explicit']:
            remove(config, installed, dep, True)

def cmd_search(args):
    config = read_config(args.config)
    index = read_package_index(config, args.dist, args.repo)
    installed = read_installed(config)
    results = find_packages(index, args.query)

    if len(results) == 0:
        print('No packages found matching "%s"' % args.query)
        return

    for result in results:
        if result in installed:
            print(result, '[Installed, %s/%s%s]' % (
                installed['dist'],
                installed['repo'],
                ', explicit' if installed['explicit'] else ''
            ))
        else:
            print(result)

def cmd_install(args):
    config = read_config(args.config)
    index = read_package_index(config, args.dist, args.repo)
    installed = read_installed(config)

    if args.system:
        for package in args.packages:
            if package in installed:
                print('Package "%s" is already installed' % package)
            else:
                installed[package] = {
                    'dist': 'special',
                    'repo': 'system',
                    'explicit': 'True',
                    'files': [],
                    'depends': []
                }

        write_installed(config, installed)
        return

    packages = []

    for package in args.packages:
        results = find_packages(index, package)

        if package in results:
            packages.append(package)
        elif len(results) > 1:
            print('Error: Abiguous package name "%s" (found %s canidates)' % (package, len(results)))
            return
        elif len(results) == 0:
            print('Error: Failed to find a package matching "%s"' % package)
            return
        else:
            packages.append(results[0])

    with tempfile.TemporaryDirectory() as tmp:
        installer = Installer(args, config, index, installed, tmp)
        for package in packages:
            installer.install(package, True)

        for root, dirs, files in os.walk(installer.install_dir):
            for file in files:
                src_path = os.path.join(root, file)
                rel_path = os.path.relpath(src_path, installer.install_dir)
                dst_path = os.path.join(config['install_root'], rel_path)
                move_file(src_path, dst_path)

        installed.update(installer.new_installed)
        write_installed(config, installed)

def cmd_remove(args):
    config = read_config(args.config)
    installed = read_installed(config)

    to_remove = []

    for package in args.packages:
        if package not in installed:
            raise Exception('Package "%s" is not installed' % package)

        remove(config, installed, package, False)
    write_installed(config, installed)

def cmd_list(args):
    config = read_config(args.config)
    installed = read_installed(config)

    for package, info in installed.items():
        print('%s, %s/%s%s' % (
            package,
            info['dist'],
            info['repo'],
            ', explicit' if info['explicit'] else ''
        ))

parser = argparse.ArgumentParser(description = 'Rip packages from the ubuntu repos')
parser.add_argument('--config', help = 'Specify apt-rip config', default = DEFAULT_CONFIG)
parser.set_defaults(subcommand = lambda args: parser.error('missing subcommand'))

subparsers = parser.add_subparsers(help = 'Specify action')

install_parser = subparsers.add_parser('install', help = 'Install packages')
install_parser.set_defaults(subcommand = cmd_install)
install_parser.add_argument('packages', nargs = '+', help = 'Packages to install')
install_parser.add_argument('--dist', help = 'Specify download distro', default = DEFAULT_DIST)
install_parser.add_argument('--repo', help = 'Specify download repo', default = DEFAULT_REPO)
install_parser.add_argument('--system', help = 'Mark packages as installed by the system', action = 'store_true')

remove_parser = subparsers.add_parser('remove', help = 'Remove packages')
remove_parser.set_defaults(subcommand = cmd_remove)
remove_parser.add_argument('packages', nargs = '+', help = 'Packages to remove')

search_parser = subparsers.add_parser('search', help = 'Search for a package')
search_parser.set_defaults(subcommand = cmd_search)
search_parser.add_argument('query', help = 'Package query to search for')

search_parser = subparsers.add_parser('list', help = 'Installed packages')
search_parser.set_defaults(subcommand = cmd_list)

args = parser.parse_args()

args.subcommand(args)
