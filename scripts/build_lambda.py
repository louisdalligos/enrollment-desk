"""Build a Linux x86_64 / Python 3.12 Lambda package from Windows or Linux."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    dependencies = project['dependencies'] + project['optional-dependencies']['aws']
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    # A fresh staging directory avoids mixing stale or host-specific dependencies.
    with tempfile.TemporaryDirectory(prefix='lambda-', dir=build) as temporary:
        stage = Path(temporary) / 'package'
        subprocess.run([
            sys.executable, '-m', 'pip', 'install', '--quiet',
            '--platform', 'manylinux2014_x86_64', '--implementation', 'cp',
            '--python-version', '3.12', '--only-binary=:all:', '--no-compile',
            '--target', str(stage), *dependencies,
        ], check=True)
        shutil.copytree(ROOT / 'src/enrollment_desk', stage / 'enrollment_desk',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        archive = Path(temporary) / 'lambda.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
            for path in sorted(stage.rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    output.write(path, path.relative_to(stage).as_posix())
        archive.replace(build / 'lambda.zip')
    print(f'Built {build / "lambda.zip"} (Linux x86_64, Python 3.12). No AWS resources changed.')


if __name__ == '__main__':
    main()
