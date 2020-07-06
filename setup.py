#!/usr/bin/env python
from __future__ import print_function

try:
    import torch
except ImportError as e:
    print('Unable to import torch. Error:')
    print('\t', e)
    print('You need to install pytorch first.')
    sys.exit(1)

from subprocess import check_call
from setuptools import setup, Extension, find_packages, distutils
from setuptools.command.build_ext import build_ext
from distutils.spawn import find_executable
from sysconfig import get_paths

import distutils.ccompiler
import distutils.command.clean
import glob
import inspect
import multiprocessing
import multiprocessing.pool
import os
import platform
import re
import shutil
import subprocess
import sys

pytorch_install_dir = os.path.dirname(os.path.abspath(torch.__file__))
base_dir = os.path.dirname(os.path.abspath(__file__))
python_include_dir = get_paths()['include']


def _check_env_flag(name, default=''):
  return os.getenv(name, default).upper() in ['ON', '1', 'YES', 'TRUE', 'Y']


def _get_env_backend():
  env_backend_var_name = 'IPEX_BACKEND'
  env_backend_options = ['cpu', 'gpu']
  env_backend_val = os.getenv(env_backend_var_name)
  if env_backend_val is None or env_backend_val.strip() == '':
    return 'cpu'
  else:
    if env_backend_val not in env_backend_options:
      print("Intel PyTorch Extension only supports CPU and GPU now.")
      sys.exit(1)
    else:
      return env_backend_val


def get_git_head_sha(base_dir):
  ipex_git_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                        cwd=base_dir).decode('ascii').strip()
  if os.path.isdir(os.path.join(base_dir, '..', '.git')):
    torch_git_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                            cwd=os.path.join(
                                                base_dir,
                                                '..')).decode('ascii').strip()
  else:
    torch_git_sha = ''
  return ipex_git_sha, torch_git_sha


def get_build_version(ipex_git_sha):
  version = os.getenv('TORCH_IPEX_VERSION', '1.0.0')
  if _check_env_flag('VERSIONED_IPEX_BUILD', default='0'):
    try:
      version += '+' + ipex_git_sha[:7]
    except Exception:
      pass
  return version


def create_version_files(base_dir, version, ipex_git_sha, torch_git_sha):
  print('Building torch_ipex version: {}'.format(version))
  py_version_path = os.path.join(base_dir, 'intel_pytorch_extension_py', 'version.py')
  with open(py_version_path, 'w') as f:
    f.write('# Autogenerated file, do not edit!\n')
    f.write("__version__ = '{}'\n".format(version))
    f.write("__ipex_gitrev__ = '{}'\n".format(ipex_git_sha))
    f.write("__torch_gitrev__ = '{}'\n".format(torch_git_sha))

  cpp_version_path = os.path.join(base_dir, 'torch_ipex', 'csrc', 'version.cpp')
  with open(cpp_version_path, 'w') as f:
    f.write('// Autogenerated file, do not edit!\n')
    f.write('#include "torch_ipex/csrc/version.h"\n\n')
    f.write('namespace torch_ipex {\n\n')
    f.write('const char IPEX_GITREV[] = {{"{}"}};\n'.format(ipex_git_sha))
    f.write('const char TORCH_GITREV[] = {{"{}"}};\n\n'.format(torch_git_sha))
    f.write('}  // namespace torch_ipex\n')


def generate_ipex_cpu_aten_code(base_dir):
  cur_dir = os.path.abspath(os.path.curdir)

  os.chdir(os.path.join(base_dir, 'scripts', 'cpu'))

  cpu_ops_path = os.path.join(base_dir, 'torch_ipex', 'csrc', 'cpu')
  sparse_dec_file_path = os.path.join(base_dir, 'scripts', 'cpu', 'pytorch_headers')
  generate_code_cmd = ['./gen-sparse-cpu-ops.sh', cpu_ops_path, pytorch_install_dir, sparse_dec_file_path]
  if subprocess.call(generate_code_cmd) != 0:
    print("Failed to run '{}'".format(generate_code_cmd), file=sys.stderr)
    os.chdir(cur_dir)
    sys.exit(1)

  generate_code_cmd = ['./gen-dense-cpu-ops.sh', cpu_ops_path, pytorch_install_dir]
  if subprocess.call(generate_code_cmd) != 0:
    print("Failed to run '{}'".format(generate_code_cmd), file=sys.stderr)
    os.chdir(cur_dir)
    sys.exit(1)

  os.chdir(cur_dir)


class IPEXExt(Extension, object):
  def __init__(self, name, project_dir=os.path.dirname(__file__)):
    Extension.__init__(self, name, sources=[])
    self.project_dir = os.path.abspath(project_dir)
    self.build_dir = os.path.join(project_dir, 'build')


class IPEXClean(distutils.command.clean.clean, object):

  def run(self):
    import glob
    import re
    with open('.gitignore', 'r') as f:
      ignores = f.read()
      pat = re.compile(r'^#( BEGIN NOT-CLEAN-FILES )?')
      for wildcard in filter(None, ignores.split('\n')):
        match = pat.match(wildcard)
        if match:
          if match.group(1):
            # Marker is found and stop reading .gitignore.
            break
          # Ignore lines which begin with '#'.
        else:
          for filename in glob.glob(wildcard):
            try:
              os.remove(filename)
            except OSError:
              shutil.rmtree(filename, ignore_errors=True)

    # It's an old-style class in Python 2.7...
    distutils.command.clean.clean.run(self)


class IPEXBuild(build_ext, object):
  def run(self):
    print("run")

    # Generate the code before globbing!
    generate_ipex_cpu_aten_code(base_dir)

    cmake = find_executable('cmake3') or find_executable('cmake')
    if cmake is None:
      raise RuntimeError(
          "CMake must be installed to build the following extensions: " +
              ", ".join(e.name for e in self.extensions))
    self.cmake = cmake

    if platform.system() == "Windows":
      raise RuntimeError("Does not support windows")

    for ext in self.extensions:
      self.build_extension(ext)

  def build_extension(self, ext):
    ext_dir = os.path.abspath(os.path.dirname(self.get_ext_fullpath(ext.name)))
    if not os.path.exists(ext.build_dir):
      os.mkdir(ext.build_dir)

    build_type = 'Release'
    use_ninja = False

    if _check_env_flag('DEBUG'):
      build_type = 'Debug'

    # install _torch_ipex.so as python module
    if ext.name is 'torch_ipex' and _check_env_flag("USE_SYCL"):
      ext_dir = ext_dir + '/torch_ipex'

    cmake_args = [
            '-DCMAKE_BUILD_TYPE=' + build_type,
            '-DPYTORCH_INSTALL_DIR=' + pytorch_install_dir,
            '-DPYTHON_EXECUTABLE=' + sys.executable,
            '-DCMAKE_INSTALL_PREFIX=' + ext_dir,
            '-DCMAKE_LIBRARY_OUTPUT_DIRECTORY=' + ext_dir,
            '-DPYTHON_INCLUDE_DIR=' + python_include_dir,
        ]

    if _check_env_flag("USE_SYCL"):
      cmake_args += ['-DUSE_SYCL=1']

    if _check_env_flag("DPCPP_ENABLE_PROFILING"):
      cmake_args += ['-DDPCPP_ENABLE_PROFILING=1']

    if _check_env_flag("USE_NINJA"):
      use_ninja = True
      cmake_args += ['-GNinja']

    build_args = ['-j', str(multiprocessing.cpu_count())]

    env = os.environ.copy()
    if _check_env_flag("USE_SYCL"):
      os.environ['CXX'] = 'compute++'
      check_call([self.cmake, ext.project_dir] + cmake_args, cwd=ext.build_dir, env=env)
    else:
      check_call([self.cmake, ext.project_dir] + cmake_args, cwd=ext.build_dir, env=env)

    # build_args += ['VERBOSE=1']
    if use_ninja:
      check_call(['ninja'] + build_args, cwd=ext.build_dir, env=env)
    else:
      check_call(['make'] + build_args, cwd=ext.build_dir, env=env)


ipex_git_sha, torch_git_sha = get_git_head_sha(base_dir)
version = get_build_version(ipex_git_sha)

# Generate version info (torch_xla.__version__)
create_version_files(base_dir, version, ipex_git_sha, torch_git_sha)


# Constant known variables used throughout this file

# PyTorch installed library
IS_WINDOWS = (platform.system() == 'Windows')
IS_DARWIN = (platform.system() == 'Darwin')
IS_LINUX = (platform.system() == 'Linux')


def make_relative_rpath(path):
  if IS_DARWIN:
    return '-Wl,-rpath,@loader_path/' + path
  elif IS_WINDOWS:
    return ''
  else:
    return '-Wl,-rpath,$ORIGIN/' + path

setup(
    name='torch_ipex',
    version=version,
    description='Intel PyTorch Extension',
    url='https://github.com/intel/intel-extension-for-pytorch',
    author='Intel/PyTorch Dev Team',
    # Exclude the build files.
    #packages=find_packages(exclude=['build']),
    packages=[
      'torch_ipex',
      'intel_pytorch_extension',
      'intel_pytorch_extension.optim',
      'intel_pytorch_extension.ops'],
    package_dir={'intel_pytorch_extension': 'intel_pytorch_extension_py'},
    zip_safe=False,
    ext_modules=[IPEXExt('_torch_ipex')],
    cmdclass={
        'build_ext': IPEXBuild,
        'clean': IPEXClean,
    })
