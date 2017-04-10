#!/usr/bin/env python
# Copyright 2017 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Main Python API for analyzing binary size."""

import argparse
import calendar
import datetime
import gzip
import logging
import os
import re
import subprocess
import sys

import describe
import file_format
import function_signature
import helpers
import linker_map_parser
import models
import ninja_parser
import paths


def _OpenMaybeGz(path, mode=None):
  """Calls `gzip.open()` if |path| ends in ".gz", otherwise calls `open()`."""
  if path.endswith('.gz'):
    if mode and 'w' in mode:
      return gzip.GzipFile(path, mode, 1)
    return gzip.open(path, mode)
  return open(path, mode or 'r')


def _UnmangleRemainingSymbols(symbol_group, tool_prefix):
  """Uses c++filt to unmangle any symbols that need it."""
  to_process = [s for s in symbol_group if s.name.startswith('_Z')]
  if not to_process:
    return

  logging.info('Unmangling %d names', len(to_process))
  proc = subprocess.Popen([tool_prefix + 'c++filt'], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE)
  stdout = proc.communicate('\n'.join(s.name for s in to_process))[0]
  assert proc.returncode == 0

  for i, line in enumerate(stdout.splitlines()):
    to_process[i].name = line


def _NormalizeNames(symbol_group):
  """Ensures that all names are formatted in a useful way.

  This includes:
    - Assigning of |full_name|.
    - Stripping of return types in |full_name| and |name| (for functions).
    - Stripping parameters from |name|.
    - Moving "vtable for" and the like to be suffixes rather than prefixes.
  """
  found_prefixes = set()
  for symbol in symbol_group:
    if symbol.name.startswith('*'):
      # See comment in _RemoveDuplicatesAndCalculatePadding() about when this
      # can happen.
      continue

    # E.g.: vtable for FOO
    idx = symbol.name.find(' for ', 0, 30)
    if idx != -1:
      found_prefixes.add(symbol.name[:idx + 4])
      symbol.name = symbol.name[idx + 5:] + ' [' + symbol.name[:idx] + ']'

    # E.g.: virtual thunk to FOO
    idx = symbol.name.find(' to ', 0, 30)
    if idx != -1:
      found_prefixes.add(symbol.name[:idx + 3])
      symbol.name = symbol.name[idx + 4:] + ' [' + symbol.name[:idx] + ']'

    # Strip out return type, and identify where parameter list starts.
    if symbol.section == 't':
      symbol.full_name, symbol.name = function_signature.Parse(symbol.name)

    # Remove anonymous namespaces (they just harm clustering).
    non_anonymous = symbol.name.replace('(anonymous namespace)::', '')
    if symbol.name != non_anonymous:
      symbol.is_anonymous = True
      symbol.name = non_anonymous
      symbol.full_name = symbol.full_name.replace(
          '(anonymous namespace)::', '')

    if symbol.section != 't' and '(' in symbol.name:
      # Pretty rare. Example:
      # blink::CSSValueKeywordsHash::findValueImpl(char const*)::value_word_list
      symbol.full_name = symbol.name
      symbol.name = re.sub(r'\(.*\)', '', symbol.full_name)

    # Don't bother storing both if they are the same.
    if symbol.full_name == symbol.name:
      symbol.full_name = ''

  logging.debug('Found name prefixes of: %r', found_prefixes)


def _NormalizeObjectPaths(symbol_group):
  """Ensures that all paths are formatted in a useful way."""
  for symbol in symbol_group:
    path = symbol.object_path
    if path.startswith('obj/'):
      # Convert obj/third_party/... -> third_party/...
      path = path[4:]
    elif path.startswith('../../'):
      # Convert ../../third_party/... -> third_party/...
      path = path[6:]
    if path.endswith(')'):
      # Convert foo/bar.a(baz.o) -> foo/bar.a/baz.o
      start_idx = path.index('(')
      path = os.path.join(path[:start_idx], path[start_idx + 1:-1])
    symbol.object_path = path


def _NormalizeSourcePath(path):
  if path.startswith('gen/'):
    # Convert gen/third_party/... -> third_party/...
    return path[4:]
  if path.startswith('../../'):
    # Convert ../../third_party/... -> third_party/...
    return path[6:]
  return path


def _ExtractSourcePaths(symbol_group, output_directory):
  """Fills in the .source_path attribute of all symbols.

  Returns True if source paths were found.
  """
  all_found = True
  mapper = ninja_parser.SourceFileMapper(output_directory)

  for symbol in symbol_group:
    object_path = symbol.object_path
    if symbol.source_path or not object_path:
      continue
    # We don't have source info for prebuilt .a files.
    if not object_path.startswith('..'):
      source_path = mapper.FindSourceForPath(object_path)
      if source_path:
        symbol.source_path = _NormalizeSourcePath(source_path)
      else:
        all_found = False
        logging.warning('Could not find source path for %s', object_path)
  logging.debug('Parsed %d .ninja files.', mapper.GetParsedFileCount())
  return all_found


def _RemoveDuplicatesAndCalculatePadding(symbol_group):
  """Removes symbols at the same address and calculates the |padding| field.

  Symbols must already be sorted by |address|.
  """
  to_remove = []
  seen_sections = []
  for i, symbol in enumerate(symbol_group[1:]):
    prev_symbol = symbol_group[i]
    if prev_symbol.section_name != symbol.section_name:
      assert symbol.section_name not in seen_sections, (
          'Input symbols must be sorted by section, then address.')
      seen_sections.append(symbol.section_name)
      continue
    if symbol.address <= 0 or prev_symbol.address <= 0:
      continue
    # Fold symbols that are at the same address (happens in nm output).
    prev_is_padding_only = prev_symbol.size_without_padding == 0
    if symbol.address == prev_symbol.address and not prev_is_padding_only:
      symbol.size = max(prev_symbol.size, symbol.size)
      to_remove.add(symbol)
      continue
    # Even with symbols at the same address removed, overlaps can still
    # happen. In this case, padding will be negative (and this is fine).
    padding = symbol.address - prev_symbol.end_address
    # These thresholds were found by manually auditing arm32 Chrome.
    # E.g.: Set them to 0 and see what warnings get logged.
    # TODO(agrieve): See if these thresholds make sense for architectures
    #     other than arm32.
    if not symbol.name.startswith('*') and (
        symbol.section in 'rd' and padding >= 256 or
        symbol.section in 't' and padding >= 64):
      # For nm data, this is caused by data that has no associated symbol.
      # The linker map file lists them with no name, but with a file.
      # Example:
      #   .data 0x02d42764 0x120 .../V8SharedWorkerGlobalScope.o
      # Where as most look like:
      #   .data.MANGLED_NAME...
      logging.debug('Large padding of %d between:\n  A) %r\n  B) %r' % (
                    padding, prev_symbol, symbol))
      continue
    symbol.padding = padding
    symbol.size += padding
    assert symbol.size >= 0, (
        'Symbol has negative size (likely not sorted propertly): '
        '%r\nprev symbol: %r' % (symbol, prev_symbol))
  # Map files have no overlaps, so worth special-casing the no-op case.
  if to_remove:
    logging.info('Removing %d overlapping symbols', len(to_remove))
    symbol_group -= models.SymbolGroup(to_remove)


def Analyze(path, lazy_paths=None):
  """Returns a SizeInfo for the given |path|.

  Args:
    path: Can be a .size file, or a .map(.gz). If the latter, then lazy_paths
        must be provided as well.
  """
  if path.endswith('.size'):
    logging.debug('Loading results from: %s', path)
    size_info = file_format.LoadSizeInfo(path)
    # Recompute derived values (padding and function names).
    logging.info('Calculating padding')
    _RemoveDuplicatesAndCalculatePadding(size_info.symbols)
    logging.info('Deriving signatures')
    # Re-parse out function parameters.
    _NormalizeNames(size_info.symbols)
    return size_info
  elif not path.endswith('.map') and not path.endswith('.map.gz'):
    raise Exception('Expected input to be a .map or a .size')
  else:
    # output_directory needed for source file information.
    lazy_paths.VerifyOutputDirectory()
    # tool_prefix needed for c++filt.
    lazy_paths.VerifyToolPrefix()

    with _OpenMaybeGz(path) as map_file:
      section_sizes, symbols = linker_map_parser.MapFileParser().Parse(map_file)
    size_info = models.SizeInfo(section_sizes, models.SymbolGroup(symbols))

    # Map file for some reason doesn't unmangle all names.
    logging.info('Calculating padding')
    _RemoveDuplicatesAndCalculatePadding(size_info.symbols)
    # Unmangle prints its own log statement.
    _UnmangleRemainingSymbols(size_info.symbols, lazy_paths.tool_prefix)
    logging.info('Extracting source paths from .ninja files')
    all_found = _ExtractSourcePaths(size_info.symbols,
                                    lazy_paths.output_directory)
    assert all_found, (
        'One or more source file paths could not be found. Likely caused by '
        '.ninja files being generated at a different time than the .map file.')
    # Resolve paths prints its own log statement.
    logging.info('Normalizing names')
    _NormalizeNames(size_info.symbols)
    logging.info('Normalizing paths')
    _NormalizeObjectPaths(size_info.symbols)

  if logging.getLogger().isEnabledFor(logging.INFO):
    for line in describe.DescribeSizeInfoCoverage(size_info):
      logging.info(line)
  logging.info('Finished analyzing %d symbols', len(size_info.symbols))
  return size_info


def _DetectGitRevision(directory):
  try:
    git_rev = subprocess.check_output(
        ['git', '-C', directory, 'rev-parse', 'HEAD'])
    return git_rev.rstrip()
  except Exception:
    logging.warning('Failed to detect git revision for file metadata.')
    return None


def BuildIdFromElf(elf_path, tool_prefix):
  args = [tool_prefix + 'readelf', '-n', elf_path]
  stdout = subprocess.check_output(args)
  match = re.search(r'Build ID: (\w+)', stdout)
  assert match, 'Build ID not found from running: ' + ' '.join(args)
  return match.group(1)


def _SectionSizesFromElf(elf_path, tool_prefix):
  args = [tool_prefix + 'readelf', '-S', '--wide', elf_path]
  stdout = subprocess.check_output(args)
  section_sizes = {}
  # Matches  [ 2] .hash HASH 00000000006681f0 0001f0 003154 04   A  3   0  8
  for match in re.finditer(r'\[[\s\d]+\] (\..*)$', stdout, re.MULTILINE):
    items = match.group(1).split()
    section_sizes[items[0]] = int(items[4], 16)
  return section_sizes


def _ParseGnArgs(args_path):
  """Returns a list of normalized "key=value" strings."""
  args = {}
  with open(args_path) as f:
    for l in f:
      # Strips #s even if within string literal. Not a problem in practice.
      parts = l.split('#')[0].split('=')
      if len(parts) != 2:
        continue
      args[parts[0].strip()] = parts[1].strip()
  return ["%s=%s" % x for x in sorted(args.iteritems())]


def main(argv):
  parser = argparse.ArgumentParser(argv)
  parser.add_argument('elf_file', help='Path to input ELF file.')
  parser.add_argument('output_file', help='Path to output .size(.gz) file.')
  parser.add_argument('--map-file',
                      help='Path to input .map(.gz) file. Defaults to '
                           '{{elf_file}}.map(.gz)?')
  paths.AddOptions(parser)
  args = helpers.AddCommonOptionsAndParseArgs(parser, argv)
  if not args.output_file.endswith('.size'):
    parser.error('output_file must end with .size')

  if args.map_file:
    map_file_path = args.map_file
  elif args.elf_file.endswith('.size'):
    # Allow a .size file to be passed as input as well. Useful for measuring
    # serialization speed.
    pass
  else:
    map_file_path = args.elf_file + '.map'
    if not os.path.exists(map_file_path):
      map_file_path += '.gz'
    if not os.path.exists(map_file_path):
      parser.error('Could not find .map(.gz)? file. Use --map-file.')

  lazy_paths = paths.LazyPaths(args=args, input_file=args.elf_file)
  metadata = None
  if args.elf_file and not args.elf_file.endswith('.size'):
    logging.debug('Constructing metadata')
    git_rev = _DetectGitRevision(os.path.dirname(args.elf_file))
    build_id = BuildIdFromElf(args.elf_file, lazy_paths.tool_prefix)
    timestamp_obj = datetime.datetime.utcfromtimestamp(os.path.getmtime(
        args.elf_file))
    timestamp = calendar.timegm(timestamp_obj.timetuple())
    gn_args = _ParseGnArgs(os.path.join(lazy_paths.output_directory, 'args.gn'))

    def relative_to_out(path):
      return os.path.relpath(path, lazy_paths.VerifyOutputDirectory())

    metadata = {
        models.METADATA_GIT_REVISION: git_rev,
        models.METADATA_MAP_FILENAME: relative_to_out(map_file_path),
        models.METADATA_ELF_FILENAME: relative_to_out(args.elf_file),
        models.METADATA_ELF_MTIME: timestamp,
        models.METADATA_ELF_BUILD_ID: build_id,
        models.METADATA_GN_ARGS: gn_args,
    }

  size_info = Analyze(map_file_path, lazy_paths)

  if metadata:
    logging.debug('Validating section sizes')
    elf_section_sizes = _SectionSizesFromElf(args.elf_file,
                                             lazy_paths.tool_prefix)
    for k, v in elf_section_sizes.iteritems():
      assert v == size_info.section_sizes.get(k), (
          'ELF file and .map file do not match.')

    size_info.metadata = metadata

  logging.info('Recording metadata: \n  %s',
               '\n  '.join(describe.DescribeMetadata(size_info.metadata)))
  logging.info('Saving result to %s', args.output_file)
  file_format.SaveSizeInfo(size_info, args.output_file)
  logging.info('Done')


if __name__ == '__main__':
  sys.exit(main(sys.argv))