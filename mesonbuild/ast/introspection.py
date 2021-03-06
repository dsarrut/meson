# Copyright 2018 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This class contains the basic functionality needed to run any interpreter
# or an interpreter-based tool

from . import AstInterpreter
from .. import compilers, environment, mesonlib, mparser, optinterpreter
from .. import coredata as cdata
from ..interpreterbase import InvalidArguments
from ..build import Executable, Jar, SharedLibrary, SharedModule, StaticLibrary
import os

build_target_functions = ['executable', 'jar', 'library', 'shared_library', 'shared_module', 'static_library', 'both_libraries']

class IntrospectionHelper:
    # mimic an argparse namespace
    def __init__(self, cross_file):
        self.cross_file = cross_file
        self.native_file = None
        self.cmd_line_options = {}

class IntrospectionInterpreter(AstInterpreter):
    # Interpreter to detect the options without a build directory
    # Most of the code is stolen from interperter.Interpreter
    def __init__(self, source_root, subdir, backend, cross_file=None, subproject='', subproject_dir='subprojects', env=None):
        super().__init__(source_root, subdir)

        options = IntrospectionHelper(cross_file)
        self.cross_file = cross_file
        if env is None:
            self.environment = environment.Environment(source_root, None, options)
        else:
            self.environment = env
        self.subproject = subproject
        self.subproject_dir = subproject_dir
        self.coredata = self.environment.get_coredata()
        self.option_file = os.path.join(self.source_root, self.subdir, 'meson_options.txt')
        self.backend = backend
        self.default_options = {'backend': self.backend}
        self.project_data = {}
        self.targets = []

        self.funcs.update({
            'add_languages': self.func_add_languages,
            'executable': self.func_executable,
            'jar': self.func_jar,
            'library': self.func_library,
            'project': self.func_project,
            'shared_library': self.func_shared_lib,
            'shared_module': self.func_shared_module,
            'static_library': self.func_static_lib,
            'both_libraries': self.func_both_lib,
        })

    def func_project(self, node, args, kwargs):
        if len(args) < 1:
            raise InvalidArguments('Not enough arguments to project(). Needs at least the project name.')

        proj_name = args[0]
        proj_vers = kwargs.get('version', 'undefined')
        proj_langs = self.flatten_args(args[1:])
        if isinstance(proj_vers, mparser.ElementaryNode):
            proj_vers = proj_vers.value
        if not isinstance(proj_vers, str):
            proj_vers = 'undefined'
        self.project_data = {'descriptive_name': proj_name, 'version': proj_vers}

        if os.path.exists(self.option_file):
            oi = optinterpreter.OptionInterpreter(self.subproject)
            oi.process(self.option_file)
            self.coredata.merge_user_options(oi.options)

        def_opts = self.flatten_args(kwargs.get('default_options', []))
        self.project_default_options = mesonlib.stringlistify(def_opts)
        self.project_default_options = cdata.create_options_dict(self.project_default_options)
        self.default_options.update(self.project_default_options)
        self.coredata.set_default_options(self.default_options, self.subproject, self.environment.cmd_line_options)

        if not self.is_subproject() and 'subproject_dir' in kwargs:
            spdirname = kwargs['subproject_dir']
            if isinstance(spdirname, str):
                self.subproject_dir = spdirname
        if not self.is_subproject():
            self.project_data['subprojects'] = []
            subprojects_dir = os.path.join(self.source_root, self.subproject_dir)
            if os.path.isdir(subprojects_dir):
                for i in os.listdir(subprojects_dir):
                    if os.path.isdir(os.path.join(subprojects_dir, i)):
                        self.do_subproject(i)

        self.coredata.init_backend_options(self.backend)
        options = {k: v for k, v in self.environment.cmd_line_options.items() if k.startswith('backend_')}

        self.coredata.set_options(options)
        self.func_add_languages(None, proj_langs, None)

    def do_subproject(self, dirname):
        subproject_dir_abs = os.path.join(self.environment.get_source_dir(), self.subproject_dir)
        subpr = os.path.join(subproject_dir_abs, dirname)
        try:
            subi = IntrospectionInterpreter(subpr, '', self.backend, cross_file=self.cross_file, subproject=dirname, subproject_dir=self.subproject_dir, env=self.environment)
            subi.analyze()
            subi.project_data['name'] = dirname
            self.project_data['subprojects'] += [subi.project_data]
        except:
            return

    def func_add_languages(self, node, args, kwargs):
        args = self.flatten_args(args)
        need_cross_compiler = self.environment.is_cross_build()
        for lang in sorted(args, key=compilers.sort_clink):
            lang = lang.lower()
            if lang not in self.coredata.compilers:
                self.environment.detect_compilers(lang, need_cross_compiler)

    def build_target(self, node, args, kwargs, targetclass):
        if not args:
            return
        kwargs = self.flatten_kwargs(kwargs, True)
        name = self.flatten_args(args)[0]
        srcqueue = [node]
        if 'sources' in kwargs:
            srcqueue += kwargs['sources']

        source_nodes = []
        while srcqueue:
            curr = srcqueue.pop(0)
            arg_node = None
            if isinstance(curr, mparser.FunctionNode):
                arg_node = curr.args
            elif isinstance(curr, mparser.ArrayNode):
                arg_node = curr.args
            elif isinstance(curr, mparser.IdNode):
                # Try to resolve the ID and append the node to the queue
                id = curr.value
                if id in self.assignments and self.assignments[id]:
                    node = self.assignments[id][0]
                    if isinstance(node, (mparser.ArrayNode, mparser.IdNode, mparser.FunctionNode)):
                        srcqueue += [node]
            if arg_node is None:
                continue
            elemetary_nodes = list(filter(lambda x: isinstance(x, (str, mparser.StringNode)), arg_node.arguments))
            srcqueue += list(filter(lambda x: isinstance(x, (mparser.FunctionNode, mparser.ArrayNode, mparser.IdNode)), arg_node.arguments))
            # Pop the first element if the function is a build target function
            if isinstance(curr, mparser.FunctionNode) and curr.func_name in build_target_functions:
                elemetary_nodes.pop(0)
            if elemetary_nodes:
                source_nodes += [curr]

        # Filter out kwargs from other target types. For example 'soversion'
        # passed to library() when default_library == 'static'.
        kwargs = {k: v for k, v in kwargs.items() if k in targetclass.known_kwargs}

        is_cross = False
        objects = []
        empty_sources = [] # Passing the unresolved sources list causes errors
        target = targetclass(name, self.subdir, self.subproject, is_cross, empty_sources, objects, self.environment, kwargs)

        self.targets += [{
            'name': target.get_basename(),
            'id': target.get_id(),
            'type': target.get_typename(),
            'defined_in': os.path.normpath(os.path.join(self.source_root, self.subdir, environment.build_filename)),
            'subdir': self.subdir,
            'build_by_default': target.build_by_default,
            'sources': source_nodes,
            'kwargs': kwargs,
            'node': node,
        }]

        return

    def build_library(self, node, args, kwargs):
        default_library = self.coredata.get_builtin_option('default_library')
        if default_library == 'shared':
            return self.build_target(node, args, kwargs, SharedLibrary)
        elif default_library == 'static':
            return self.build_target(node, args, kwargs, StaticLibrary)
        elif default_library == 'both':
            return self.build_target(node, args, kwargs, SharedLibrary)

    def func_executable(self, node, args, kwargs):
        return self.build_target(node, args, kwargs, Executable)

    def func_static_lib(self, node, args, kwargs):
        return self.build_target(node, args, kwargs, StaticLibrary)

    def func_shared_lib(self, node, args, kwargs):
        return self.build_target(node, args, kwargs, SharedLibrary)

    def func_both_lib(self, node, args, kwargs):
        return self.build_target(node, args, kwargs, SharedLibrary)

    def func_shared_module(self, node, args, kwargs):
        return self.build_target(node, args, kwargs, SharedModule)

    def func_library(self, node, args, kwargs):
        return self.build_library(node, args, kwargs)

    def func_jar(self, node, args, kwargs):
        return self.build_target(node, args, kwargs, Jar)

    def func_build_target(self, node, args, kwargs):
        if 'target_type' not in kwargs:
            return
        target_type = kwargs.pop('target_type')
        if isinstance(target_type, mparser.ElementaryNode):
            target_type = target_type.value
        if target_type == 'executable':
            return self.build_target(node, args, kwargs, Executable)
        elif target_type == 'shared_library':
            return self.build_target(node, args, kwargs, SharedLibrary)
        elif target_type == 'static_library':
            return self.build_target(node, args, kwargs, StaticLibrary)
        elif target_type == 'both_libraries':
            return self.build_target(node, args, kwargs, SharedLibrary)
        elif target_type == 'library':
            return self.build_library(node, args, kwargs)
        elif target_type == 'jar':
            return self.build_target(node, args, kwargs, Jar)

    def is_subproject(self):
        return self.subproject != ''

    def analyze(self):
        self.load_root_meson_file()
        self.sanity_check_ast()
        self.parse_project()
        self.run()
