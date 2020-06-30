from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional
import re
import sys

from .cmd import stream_output, collect_output


DBT_REPO = 'git@github.com:fishtown-analytics/dbt.git'
HOMEBREW_DBT_REPO = 'git@github.com:fishtown-analytics/homebrew-dbt.git'

# This should match the pattern in .bumpversion.cfg
VERSION_PATTERN_STR = (
    r'(?P<major>\d+)'
    r'\.(?P<minor>\d+)'
    r'\.(?P<patch>\d+)'
    r'((?P<prerelease>[a-z]+)(?P<num>\d+))?'
)

VERSION_PATTERN = re.compile(VERSION_PATTERN_STR)


class Version:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        match = VERSION_PATTERN.match(self.raw)
        if match is None:
            raise ValueError(f'Invalid verssion: {self.raw}')
        groups = match.groupdict()

        self.major: int = int(groups['major'])
        self.minor: int = int(groups['minor'])
        self.patch: int = int(groups['patch'])
        self.prerelease: Optional[str] = None
        self.num: Optional[int] = None

        if groups['num'] is not None:
            self.prerelease = groups['prerelease']
            self.num = int(groups['num'])

    def __str__(self):
        return self.raw

    def homebrew_class_name(self) -> str:
        name = f'DbtAT{self.major}{self.minor}{self.patch}'
        if self.prerelease is not None and self.num is not None:
            name = f'{name}{self.prerelease.title()}{self.num}'
        return name

    def homebrew_filename(self):
        version_str = f'{self.major}.{self.minor}.{self.patch}'
        if self.prerelease is not None and self.num is not None:
            version_str = f'{version_str}-{self.prerelease}{self.num}'
        return f'dbt@{version_str}.rb'


class EnvironmentInformation:
    def __init__(self):
        self.artifacts_dir = Path.cwd() / 'artifacts'
        self.build_dir = Path.cwd() / 'build'

    @property
    def dbt_dir(self) -> Path:
        return self.build_dir / 'dbt'

    @property
    def dist_dir(self) -> Path:
        return self.artifacts_dir / 'dist'

    @property
    def release_file(self) -> Path:
        return self.artifacts_dir / 'release.txt'

    @property
    def homebrew_template_pickle(self) -> Path:
        return self.artifacts_dir / 'homebrew_template.pickle'

    @property
    def wheel_file(self) -> Path:
        return self.artifacts_dir / 'wheel_requirements.txt'

    @property
    def integration_test_dir(self) -> Path:
        return self.dbt_dir / 'test/integration'

    @property
    def linux_requirements_venv(self):
        return self.build_dir / 'linux_requirements_venv'

    @property
    def test_venv(self) -> Path:
        return self.build_dir / 'test_venv'

    @property
    def packaging_venv(self) -> Path:
        return self.build_dir / 'pkg_venv'

    @property
    def homebrew_test_venv(self) -> Path:
        return self.build_dir / 'homebrew_test_venv'

    @property
    def homebrew_release_venv(self) -> Path:
        return self.build_dir / 'homebrew_release_venv'

    @property
    def homebrew_checkout_path(self) -> Path:
        return self.build_dir / 'homebrew-dbt'

    def get_dbt_requirements_file(self, version: str) -> Path:
        return self.dbt_dir / f'docker/requirements/requirements.{version}.txt'


@dataclass
class ReleaseFile:
    path: Path
    commit: str
    version: Version
    branch: str
    notes: str

    @property
    def is_prerelease(self) -> bool:
        return self.version.prerelease is not None

    @classmethod
    def from_path(cls, path: Path) -> 'ReleaseFile':
        with path.open() as fp:
            first_lines = ''.join(fp.readline() for _ in range(3))
            notes = fp.read().strip()
        version = re.search(r'version: (.*)\n', first_lines)
        if not version:
            raise ValueError(
                f'No version found in first 3 lines: {first_lines}'
            )

        commit = re.search(r'commit: (.*)\n', first_lines)
        if not commit:
            raise ValueError(
                f'No commit found in first 3 lines: {first_lines}'
            )

        branch = re.search(r'branch: (.*)\n', first_lines)
        if not branch:
            raise ValueError(
                f'No branch found in first 3 lines: {first_lines}'
            )
        return cls(
            path=path,
            version=Version(version.group(1)),
            commit=commit.group(1),
            branch=branch.group(1),
            notes=notes,
        )

    @staticmethod
    def _git_modified_files_last_commit() -> List[Path]:
        cmd = ['git', 'diff', '--name-status', 'HEAD~1', 'HEAD']
        result = collect_output(cmd)
        paths = []
        for line in result.strip().split('\n'):
            if line[0] not in 'AM':
                continue
            match = re.match(r'^[AM]\s*(releases/.*)', line)
            if match is None:
                continue
            path = Path(match.group(1))
            paths.append(path)
        return paths

    @classmethod
    def from_git(cls) -> 'ReleaseFile':
        found = []
        for path in cls._git_modified_files_last_commit():
            try:
                release = cls.from_path(path)
            except ValueError as exc:
                print(f'Found an invalid release file, skipping it: {exc}',
                      file=sys.stderr)
            else:
                found.append(release)
        if len(found) > 1:
            paths = ', '.join(str(r.path) for r in found)
            raise ValueError(
                f'Found {len(found)} releases (paths: [{paths}]), expected 1'
            )
        elif len(found) < 1:
            raise ValueError('Found 0 releases, expected 1')
        return found[0]

    def store_artifacts(self, env: EnvironmentInformation):
        print(f'Storing release file in {env.artifacts_dir}')
        env.artifacts_dir.mkdir(parents=True, exist_ok=True)
        dest = env.release_file
        with dest.open('w') as fp:
            fp.write(f'commit: {self.commit}\n')
            fp.write(f'version: {self.version}\n')
            fp.write(f'branch: {self.branch}\n')
            fp.write('\n')
            fp.write(self.notes)

    @classmethod
    def from_artifacts(cls, env: EnvironmentInformation) -> 'ReleaseFile':
        return cls.from_path(env.release_file)


class PackageType(Enum):
    Wheel = 1
    Sdist = 2

    @property
    def suffix(self):
        if self == self.Wheel:
            return '.whl'
        elif self == self.Sdist:
            return '.tar.gz'
        else:
            raise ValueError(f'Unknown {type(self)}: {self}')

    @property
    def glob(self):
        return '*' + self.suffix


class PytestRunner:
    def __init__(self, env_path: Path, dbt_path: Path):
        self.env_path = env_path
        self.dbt_path = dbt_path

    def _pytest_cmd(self, *extra: str):
        test_python_path = (self.env_path / 'bin/python').absolute()
        cmd = [
            test_python_path, '-m', 'pytest',
            '--durations', '0', '-v', '-n4',
        ]
        cmd.extend(extra)
        return cmd

    def test(self, name: str):
        if name == 'rpc':
            # RPC tests first
            cmd = self._pytest_cmd('test/rpc')
            startmsg = 'Running RPC tests'
            endmsg = 'RPC tests passed'
        else:
            cmd = self._pytest_cmd(
                '-m', f'profile_{name}',
                # we already ran integration tests, we just want to make sure
                # the package we built is functional.
                'test/integration/029_docs_generate_tests'
            )
            startmsg = f'Running tests for plugin: {name}'
            endmsg = f'Tests for plugin: {name} passed'

        print(startmsg)
        stream_output(cmd, cwd=self.dbt_path)
        print(endmsg)