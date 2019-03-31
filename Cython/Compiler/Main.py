#
#   Cython Top Level
#

from __future__ import absolute_import

import os
import re
import sys
import io

if sys.version_info[:2] < (2, 7) or (3, 0) <= sys.version_info[:2] < (3, 3):
    sys.stderr.write("Sorry, Cython requires Python 2.7 or 3.3+, found %d.%d\n" % tuple(sys.version_info[:2]))
    sys.exit(1)

try:
    from __builtin__ import basestring
except ImportError:
    basestring = str

# Do not import Parsing here, import it when needed, because Parsing imports
# Nodes, which globally needs debug command line options initialized to set a
# conditional metaclass. These options are processed by CmdLine called from
# main() in this file.
# import Parsing
from . import Errors
from .StringEncoding import EncodedString
from .Scanning import PyrexScanner, FileSourceDescriptor
from .Errors import PyrexError, CompileError, error, warning
from .Symtab import ModuleScope
from .. import Utils
from . import Options
from .Options import CompilationOptions, default_options
from .CmdLine import parse_command_line

from . import Version  # legacy import needed by old PyTables versions
version = Version.version  # legacy attribute - use "Cython.__version__" instead

module_name_pattern = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

verbose = 0

standard_include_path = os.path.abspath(os.path.normpath(
        os.path.join(os.path.dirname(__file__), os.path.pardir, 'Includes')))

class Context(object):
    #  This class encapsulates the context needed for compiling
    #  one or more Cython implementation files along with their
    #  associated and imported declaration files. It includes
    #  the root of the module import namespace and the list
    #  of directories to search for include files.
    #
    #  modules               {string : ModuleScope}
    #  include_directories   [string]
    #  future_directives     [object]
    #  language_level        int     currently 2 or 3 for Python 2/3

    cython_scope = None
    language_level = None  # warn when not set but default to Py2

    def __init__(self, include_directories, compiler_directives, cpp=False,
                 language_level=None, options=None):
        # cython_scope is a hack, set to False by subclasses, in order to break
        # an infinite loop.
        # Better code organization would fix it.

        from . import Builtin, CythonScope
        self.modules = {"__builtin__" : Builtin.builtin_scope}
        self.cython_scope = CythonScope.create_cython_scope(self)
        self.modules["cython"] = self.cython_scope
        self.include_directories = tuple(include_directories)
        self.future_directives = set()
        self.compiler_directives = compiler_directives
        self.cpp = cpp
        self.options = options

        self.pxds = {}  # full name -> node tree
        self._interned = {}  # (type(value), value, *key_args) -> interned_value

        if language_level is not None:
            self.set_language_level(language_level)

        self.gdb_debug_outputwriter = None

    @classmethod
    def from_options(cls, options):
        return cls(options.include_path, options.compiler_directives,
                   options.cplus, options.language_level, options=options)

    def set_language_level(self, level):
        from .Future import print_function, unicode_literals, absolute_import, division, generator_stop
        future_directives = set()
        if level == '3str':
            level = 3
        else:
            level = int(level)
            if level >= 3:
                future_directives.add(unicode_literals)
        if level >= 3:
            future_directives.update([print_function, absolute_import, division, generator_stop])
        self.language_level = level
        self.future_directives = future_directives
        if level >= 3:
            self.modules['builtins'] = self.modules['__builtin__']

    def intern_ustring(self, value, encoding=None):
        key = (EncodedString, value, encoding)
        try:
            return self._interned[key]
        except KeyError:
            pass
        value = EncodedString(value)
        if encoding:
            value.encoding = encoding
        self._interned[key] = value
        return value

    # pipeline creation functions can now be found in Pipeline.py

    def process_pxd(self, source_desc, scope, module_name):
        from . import Pipeline
        if isinstance(source_desc, FileSourceDescriptor) and source_desc._file_type == 'pyx':
            source = CompilationSource(source_desc, module_name, os.getcwd())
            result_sink = create_default_resultobj(source, self.options)
            pipeline = Pipeline.create_pyx_as_pxd_pipeline(self, result_sink)
            result = Pipeline.run_pipeline(pipeline, source)
        else:
            pipeline = Pipeline.create_pxd_pipeline(self, scope, module_name)
            result = Pipeline.run_pipeline(pipeline, source_desc)
        return result

    def nonfatal_error(self, exc):
        return Errors.report_error(exc)

    def find_module(self, module_name, relative_to=None, pos=None, need_pxd=1,
                    absolute_fallback=True):
        # Finds and returns the module scope corresponding to
        # the given relative or absolute module name. If this
        # is the first time the module has been requested, finds
        # the corresponding .pxd file and process it.
        # If relative_to is not None, it must be a module scope,
        # and the module will first be searched for relative to
        # that module, provided its name is not a dotted name.
        debug_find_module = 0
        if debug_find_module:
            print("Context.find_module: module_name = %s, relative_to = %s, pos = %s, need_pxd = %s" % (
                module_name, relative_to, pos, need_pxd))

        scope = None
        pxd_pathname = None
        if relative_to:
            if module_name:
                # from .module import ...
                qualified_name = relative_to.qualify_name(module_name)
            else:
                # from . import ...
                qualified_name = relative_to.qualified_name
                scope = relative_to
                relative_to = None
        else:
            qualified_name = module_name

        if not module_name_pattern.match(qualified_name):
            raise CompileError(pos or (module_name, 0, 0),
                               "'%s' is not a valid module name" % module_name)

        if relative_to:
            if debug_find_module:
                print("...trying relative import")
            scope = relative_to.lookup_submodule(module_name)
            if not scope:
                pxd_pathname = self.find_pxd_file(qualified_name, pos)
                if pxd_pathname:
                    scope = relative_to.find_submodule(module_name)
        if not scope:
            if debug_find_module:
                print("...trying absolute import")
            if absolute_fallback:
                qualified_name = module_name
            scope = self
            for name in qualified_name.split("."):
                scope = scope.find_submodule(name)

        if debug_find_module:
            print("...scope = %s" % scope)
        if not scope.pxd_file_loaded:
            if debug_find_module:
                print("...pxd not loaded")
            if not pxd_pathname:
                if debug_find_module:
                    print("...looking for pxd file")
                # Only look in sys.path if we are explicitly looking
                # for a .pxd file.
                pxd_pathname = self.find_pxd_file(qualified_name, pos, sys_path=need_pxd)
                if debug_find_module:
                    print("......found %s" % pxd_pathname)
                if not pxd_pathname and need_pxd:
                    # Set pxd_file_loaded such that we don't need to
                    # look for the non-existing pxd file next time.
                    scope.pxd_file_loaded = True
                    package_pathname = self.search_include_directories(qualified_name, ".py", pos)
                    if package_pathname and package_pathname.endswith(Utils.PACKAGE_FILES):
                        pass
                    else:
                        error(pos, "'%s.pxd' not found" % qualified_name.replace('.', os.sep))
            if pxd_pathname:
                scope.pxd_file_loaded = True
                try:
                    if debug_find_module:
                        print("Context.find_module: Parsing %s" % pxd_pathname)
                    rel_path = module_name.replace('.', os.sep) + os.path.splitext(pxd_pathname)[1]
                    if not pxd_pathname.endswith(rel_path):
                        rel_path = pxd_pathname  # safety measure to prevent printing incorrect paths
                    source_desc = FileSourceDescriptor(pxd_pathname, rel_path)
                    err, result = self.process_pxd(source_desc, scope, qualified_name)
                    if err:
                        raise err
                    (pxd_codenodes, pxd_scope) = result
                    self.pxds[module_name] = (pxd_codenodes, pxd_scope)
                except CompileError:
                    pass
        return scope

    def find_pxd_file(self, qualified_name, pos, sys_path=True):
        # Search include path (and sys.path if sys_path is True) for
        # the .pxd file corresponding to the given fully-qualified
        # module name.
        # Will find either a dotted filename or a file in a
        # package directory. If a source file position is given,
        # the directory containing the source file is searched first
        # for a dotted filename, and its containing package root
        # directory is searched first for a non-dotted filename.
        pxd = self.search_include_directories(qualified_name, ".pxd", pos, sys_path=sys_path)
        if pxd is None and Options.cimport_from_pyx:
            return self.find_pyx_file(qualified_name, pos)
        return pxd

    def find_pyx_file(self, qualified_name, pos):
        # Search include path for the .pyx file corresponding to the
        # given fully-qualified module name, as for find_pxd_file().
        return self.search_include_directories(qualified_name, ".pyx", pos)

    def find_include_file(self, filename, pos):
        # Search list of include directories for filename.
        # Reports an error and returns None if not found.
        path = self.search_include_directories(filename, "", pos,
                                               include=True)
        if not path:
            error(pos, "'%s' not found" % filename)
        return path

    def search_include_directories(self, qualified_name, suffix, pos,
                                   include=False, sys_path=False):
        if sys_path:
            include_dirs = self.include_directories + tuple(sys.path)
        else:
            include_dirs = self.include_directories
        include_dirs = include_dirs + (standard_include_path,)
        return search_include_directories(include_dirs, qualified_name,
                                          suffix, pos, include)

    def find_root_package_dir(self, file_path):
        return Utils.find_root_package_dir(file_path)

    def check_package_dir(self, dir, package_names):
        return Utils.check_package_dir(dir, tuple(package_names))

    def c_file_out_of_date(self, source_path, output_path):
        if not os.path.exists(output_path):
            return 1
        c_time = Utils.modification_time(output_path)
        if Utils.file_newer_than(source_path, c_time):
            return 1
        pos = [source_path]
        pxd_path = Utils.replace_suffix(source_path, ".pxd")
        if os.path.exists(pxd_path) and Utils.file_newer_than(pxd_path, c_time):
            return 1
        for kind, name in self.read_dependency_file(source_path):
            if kind == "cimport":
                # missing suffix?
                dep_path = self.find_pxd_file(name, pos)
            elif kind == "include":
                # missing suffix?
                dep_path = self.search_include_directories(name, pos)
            else:
                continue
            if dep_path and Utils.file_newer_than(dep_path, c_time):
                return 1
        return 0

    def find_cimported_module_names(self, source_path):
        return [ name for kind, name in self.read_dependency_file(source_path)
                 if kind == "cimport" ]

    def is_package_dir(self, dir_path):
        return Utils.is_package_dir(dir_path)

    def read_dependency_file(self, source_path):
        dep_path = Utils.replace_suffix(source_path, ".dep")
        if os.path.exists(dep_path):
            f = open(dep_path, "rU")
            chunks = [ line.strip().split(" ", 1)
                       for line in f.readlines()
                       if " " in line.strip() ]
            f.close()
            return chunks
        else:
            return ()

    def lookup_submodule(self, name):
        # Look up a top-level module. Returns None if not found.
        return self.modules.get(name, None)

    def find_submodule(self, name):
        # Find a top-level module, creating a new one if needed.
        scope = self.lookup_submodule(name)
        if not scope:
            scope = ModuleScope(name,
                parent_module = None, context = self)
            self.modules[name] = scope
        return scope

    def parse(self, source_desc, scope, pxd, full_module_name):
        if not isinstance(source_desc, FileSourceDescriptor):
            raise RuntimeError("Only file sources for code supported")
        source_filename = source_desc.filename
        scope.cpp = self.cpp
        # Parse the given source file and return a parse tree.
        num_errors = Errors.num_errors
        try:
            with Utils.open_source_file(source_filename) as f:
                from . import Parsing
                s = PyrexScanner(f, source_desc, source_encoding = f.encoding,
                                 scope = scope, context = self)
                tree = Parsing.p_module(s, pxd, full_module_name)
                if self.options.formal_grammar:
                    try:
                        from ..Parser import ConcreteSyntaxTree
                    except ImportError:
                        raise RuntimeError(
                            "Formal grammar can only be used with compiled Cython with an available pgen.")
                    ConcreteSyntaxTree.p_module(source_filename)
        except UnicodeDecodeError as e:
            #import traceback
            #traceback.print_exc()
            raise self._report_decode_error(source_desc, e)

        if Errors.num_errors > num_errors:
            raise CompileError()
        return tree

    def _report_decode_error(self, source_desc, exc):
        msg = exc.args[-1]
        position = exc.args[2]
        encoding = exc.args[0]

        line = 1
        column = idx = 0
        with io.open(source_desc.filename, "r", encoding='iso8859-1', newline='') as f:
            for line, data in enumerate(f, 1):
                idx += len(data)
                if idx >= position:
                    column = position - (idx - len(data)) + 1
                    break

        return error((source_desc, line, column),
                     "Decoding error, missing or incorrect coding=<encoding-name> "
                     "at top of source (cannot decode with encoding %r: %s)" % (encoding, msg))

    def extract_module_name(self, path, options):
        # Find fully_qualified module name from the full pathname
        # of a source file.
        dir, filename = os.path.split(path)
        module_name, _ = os.path.splitext(filename)
        if "." in module_name:
            return module_name
        names = [module_name]
        while self.is_package_dir(dir):
            parent, package_name = os.path.split(dir)
            if parent == dir:
                break
            names.append(package_name)
            dir = parent
        names.reverse()
        return ".".join(names)

    def setup_errors(self, options, result):
        Errors.reset()  # clear any remaining error state
        if options.use_listing_file:
            path = result.listing_file = Utils.replace_suffix(result.main_source_file, ".lis")
        else:
            path = None
        Errors.open_listing_file(path=path,
                                 echo_to_stderr=options.errors_to_stderr)

    def teardown_errors(self, err, options, result):
        source_desc = result.compilation_source.source_desc
        if not isinstance(source_desc, FileSourceDescriptor):
            raise RuntimeError("Only file sources for code supported")
        Errors.close_listing_file()
        result.num_errors = Errors.num_errors
        if result.num_errors > 0:
            err = True
        if err and result.c_file:
            try:
                Utils.castrate_file(result.c_file, os.stat(source_desc.filename))
            except EnvironmentError:
                pass
            result.c_file = None


def get_output_filename(source_filename, cwd, options):
    if options.cplus:
        c_suffix = ".cpp"
    else:
        c_suffix = ".c"
    suggested_file_name = Utils.replace_suffix(source_filename, c_suffix)
    if options.output_file:
        out_path = os.path.join(cwd, options.output_file)
        if os.path.isdir(out_path):
            return os.path.join(out_path, os.path.basename(suggested_file_name))
        else:
            return out_path
    else:
        return suggested_file_name


def create_default_resultobj(compilation_source, options):
    result = CompilationResult()
    result.main_source_file = compilation_source.source_desc.filename
    result.compilation_source = compilation_source
    source_desc = compilation_source.source_desc
    result.c_file = get_output_filename(source_desc.filename,
                        compilation_source.cwd, options)
    result.embedded_metadata = options.embedded_metadata
    return result


def run_pipeline(source, options, full_module_name=None, context=None):
    from . import Pipeline

    source_ext = os.path.splitext(source)[1]
    options.configure_language_defaults(source_ext[1:]) # py/pyx
    if context is None:
        context = Context.from_options(options)

    # Set up source object
    cwd = os.getcwd()
    abs_path = os.path.abspath(source)
    full_module_name = full_module_name or context.extract_module_name(source, options)

    Utils.raise_error_if_module_name_forbidden(full_module_name)

    if options.relative_path_in_code_position_comments:
        rel_path = full_module_name.replace('.', os.sep) + source_ext
        if not abs_path.endswith(rel_path):
            rel_path = source # safety measure to prevent printing incorrect paths
    else:
        rel_path = abs_path
    source_desc = FileSourceDescriptor(abs_path, rel_path)
    source = CompilationSource(source_desc, full_module_name, cwd)

    # Set up result object
    result = create_default_resultobj(source, options)

    if options.annotate is None:
        # By default, decide based on whether an html file already exists.
        html_filename = os.path.splitext(result.c_file)[0] + ".html"
        if os.path.exists(html_filename):
            with io.open(html_filename, "r", encoding="UTF-8") as html_file:
                if u'<!-- Generated by Cython' in html_file.read(100):
                    options.annotate = True

    # Get pipeline
    if source_ext.lower() == '.py' or not source_ext:
        pipeline = Pipeline.create_py_pipeline(context, options, result)
    else:
        pipeline = Pipeline.create_pyx_pipeline(context, options, result)

    context.setup_errors(options, result)
    err, enddata = Pipeline.run_pipeline(pipeline, source)
    context.teardown_errors(err, options, result)
    return result


# ------------------------------------------------------------------------
#
#  Main Python entry points
#
# ------------------------------------------------------------------------

class CompilationSource(object):
    """
    Contains the data necessary to start up a compilation pipeline for
    a single compilation unit.
    """
    def __init__(self, source_desc, full_module_name, cwd):
        self.source_desc = source_desc
        self.full_module_name = full_module_name
        self.cwd = cwd


class CompilationResult(object):
    """
    Results from the Cython compiler:

    c_file           string or None   The generated C source file
    h_file           string or None   The generated C header file
    i_file           string or None   The generated .pxi file
    api_file         string or None   The generated C API .h file
    listing_file     string or None   File of error messages
    object_file      string or None   Result of compiling the C file
    extension_file   string or None   Result of linking the object file
    num_errors       integer          Number of compilation errors
    compilation_source CompilationSource
    """

    def __init__(self):
        self.c_file = None
        self.h_file = None
        self.i_file = None
        self.api_file = None
        self.listing_file = None
        self.object_file = None
        self.extension_file = None
        self.main_source_file = None


class CompilationResultSet(dict):
    """
    Results from compiling multiple Pyrex source files. A mapping
    from source file paths to CompilationResult instances. Also
    has the following attributes:

    num_errors   integer   Total number of compilation errors
    """

    num_errors = 0

    def add(self, source, result):
        self[source] = result
        self.num_errors += result.num_errors


def compile_single(source, options, full_module_name = None):
    """
    compile_single(source, options, full_module_name)

    Compile the given Pyrex implementation file and return a CompilationResult.
    Always compiles a single file; does not perform timestamp checking or
    recursion.
    """
    return run_pipeline(source, options, full_module_name)


def compile_multiple(sources, options):
    """
    compile_multiple(sources, options)

    Compiles the given sequence of Pyrex implementation files and returns
    a CompilationResultSet. Performs timestamp checking and/or recursion
    if these are specified in the options.
    """
    # run_pipeline creates the context
    # context = Context.from_options(options)
    sources = [os.path.abspath(source) for source in sources]
    processed = set()
    results = CompilationResultSet()
    timestamps = options.timestamps
    verbose = options.verbose
    context = None
    cwd = os.getcwd()
    for source in sources:
        if source not in processed:
            if context is None:
                context = Context.from_options(options)
            output_filename = get_output_filename(source, cwd, options)
            out_of_date = context.c_file_out_of_date(source, output_filename)
            if (not timestamps) or out_of_date:
                if verbose:
                    sys.stderr.write("Compiling %s\n" % source)

                result = run_pipeline(source, options, context=context)
                results.add(source, result)
                # Compiling multiple sources in one context doesn't quite
                # work properly yet.
                context = None
            processed.add(source)
    return results


def compile(source, options = None, full_module_name = None, **kwds):
    """
    compile(source [, options], [, <option> = <value>]...)

    Compile one or more Pyrex implementation files, with optional timestamp
    checking and recursing on dependencies.  The source argument may be a string
    or a sequence of strings.  If it is a string and no recursion or timestamp
    checking is requested, a CompilationResult is returned, otherwise a
    CompilationResultSet is returned.
    """
    options = CompilationOptions(defaults = options, **kwds)
    if isinstance(source, basestring) and not options.timestamps:
        return compile_single(source, options, full_module_name)
    else:
        return compile_multiple(source, options)


@Utils.cached_function
def search_include_directories(dirs, qualified_name, suffix, pos, include=False):
    """
    Search the list of include directories for the given file name.

    If a source file position is given, first searches the directory
    containing that file. Returns None if not found, but does not
    report an error.

    The 'include' option will disable package dereferencing.
    """

    if pos:
        file_desc = pos[0]
        if not isinstance(file_desc, FileSourceDescriptor):
            raise RuntimeError("Only file sources for code supported")
        if include:
            dirs = (os.path.dirname(file_desc.filename),) + dirs
        else:
            dirs = (Utils.find_root_package_dir(file_desc.filename),) + dirs

    dotted_filename = qualified_name
    if suffix:
        dotted_filename += suffix

    if not include:
        names = qualified_name.split('.')
        package_names = tuple(names[:-1])
        module_name = names[-1]
        module_filename = module_name + suffix
        package_filename = "__init__" + suffix

    for dirname in dirs:
        path = os.path.join(dirname, dotted_filename)
        if os.path.exists(path):
            return path

        if not include:
            package_dir = Utils.check_package_dir(dirname, package_names)
            if package_dir is not None:
                path = os.path.join(package_dir, module_filename)
                if os.path.exists(path):
                    return path
                # In most cases, dirname and package_dir will be the same.
                # From the documentation of os.path.join:
                # " If a component is an absolute path, all previous components
                # are thrown away and joining continues from the absolute path
                # component"
                # So if dirname and package_dir are absolute pathes, one will
                # be discarded. However what happens when they are relative
                # single-component paths? They will be concatenated (repeated),
                # causing rare and hard to debug problems.
                # path = os.path.join(dirname, package_dir, module_name,
                #                    package_filename)
                path = os.path.join(package_dir, module_name,
                                    package_filename)
                if os.path.exists(path):
                    return path
    return None


# ------------------------------------------------------------------------
#
#  Main command-line entry point
#
# ------------------------------------------------------------------------

def setuptools_main():
    return main(command_line = 1)


def main(command_line = 0):
    args = sys.argv[1:]
    any_failures = 0
    if command_line:
        options, sources = parse_command_line(args)
    else:
        options = CompilationOptions(default_options)
        sources = args

    if options.show_version:
        sys.stderr.write("Cython version %s\n" % version)
    if options.working_path!="":
        os.chdir(options.working_path)
    try:
        result = compile(sources, options)
        if result.num_errors > 0:
            any_failures = 1
    except (EnvironmentError, PyrexError) as e:
        sys.stderr.write(str(e) + '\n')
        any_failures = 1
    if any_failures:
        sys.exit(1)
