# Copyright 2020 Lawrence Livermore National Security, LLC and other CLIPPy Project Developers.
# See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: MIT

import json
import logging
import sys
import types
import uuid
import inspect
from subprocess import run
from clippy.error import ClippyBackendError, ClippyValidationError
from clippy.regcommand import get_registered_commands
from clippy import config, AnyDict
from clippy.serialization import encode_clippy_json, decode_clippy_json

from typing import Callable, List, Optional, Sequence, Tuple, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from subprocess import CompletedProcess


#  Set this to the clippy executable flag that does validation of stdin.
DRY_RUN_FLAG: str = '--clippy-validate'


def _exec(execcmd: Union[str, Sequence[str]], submission_dict: AnyDict, logger: logging.Logger) -> CompletedProcess:
    '''
    Internal function.

    Executes the command specified with `execcmd` and
    passes `submission_dict` as JSON via STDIN.

    Logs debug messages with progress.
    Returns the process result object.

    This function is used by _run and _validate.
    '''

    logger.debug(f'Submission = {submission_dict}')
    # PP support passing objects
    # ~ cmd_stdin = json.dumps(submission_dict)
    cmd_stdin = json.dumps(submission_dict, default=encode_clippy_json)
    logger.debug(f'Calling {execcmd} with input {cmd_stdin}')
    p = run(execcmd, input=cmd_stdin, capture_output=True, encoding='utf-8')
    logger.debug(f'run(): result = {p}')
    return p


class Command():
    '''
    An object that contains information relating to valid back-end commands.
    '''

    def __init__(self, namespace: str, name: str, session: "Clippy", jsondict: AnyDict):
        self.namespace = namespace
        self.name = name  # name of method
        self.session = session
        self.jsondict = jsondict
        self.positionals: AnyDict = {}
        for (arg, argparams) in self.args.items():   # self.args is defined below as a @property
            if argparams.get('position', -1) != -1:  # if position is not -1, this is a 0-indexed positional arg.
                self.session.logger.debug(f'  adding positional {arg} at {argparams["position"]}.')
                self.positionals[argparams['position']] = arg

    def genfn(self, docstring: str) -> Callable:
        '''
        Generates a function and sets its docstring.
        The returned function is used to dynamically
        create a method for a Clippy instance.
        '''

        # closure for self for the internal method, since it will be
        # shadowed within fn() otherwise.
        capself = self

        def fn(self, *args, **kwargs):
            # args are positional parameters, kwargs are named.
            # these are arguments to the command itself.
            self.logger.debug('Running Subcommand: ' + capself.name)
            self.logger.debug(f'  args = {args}, kwargs = {kwargs}')
            self.logger.debug(f'  positionals = {capself.positionals}')

            for (i, arg) in enumerate(args):
                if i not in capself.positionals:
                    self.logger.warn(f'Invalid positional argument "{arg}". Ignoring.')
                else:
                    posarg_name = capself.positionals[i]
                    if posarg_name in kwargs:  # we don't override dictionary with positionals.
                        self.logger.warn(
                            f'Positional argument "{arg}" conflicts with dictionary argument \
{posarg_name} "{kwargs[posarg_name]}"; ignoring.'
                        )
                    else:
                        kwargs[posarg_name] = arg

            (valid, warnings) = capself.session._validate(capself.exe_name, kwargs)
            # if validate doesn't throw an error, we either have True (successful validation) or False
            # (warnings).
            if valid:  # no validation errors or warnings
                return capself.session._run(capself.exe_name, kwargs)
            else:
                self.logger.warn(f'Validation returned warning: {warnings}; aborting execution')
                return {}

        f = fn
        f.__doc__ = docstring
        return f

    @property
    def args(self) -> AnyDict:
        return self.jsondict.get('args', {})

    @property
    def docstring(self) -> str:
        # we're probably overallocating here but whatever.
        posargs: List[Optional[Tuple[str, AnyDict]]] = [None] * len(self.args)
        optargs: List[Tuple[str, AnyDict]] = []
        numpos = -1

        # sort the args into positional and optional.
        for arg, v in self.args.items():
            pos = v.get('position', -1)
            if pos == -1:
                optargs.append((arg, v))
            else:
                posargs[pos] = (arg, v)
                numpos = max(numpos, pos)

        numpos += 1
        posargs = posargs[:numpos]

        # make sure we have contiguous positional arguments.
        for p in posargs:
            if p is None:
                raise ClippyBackendError(f'Invalid options received for {self.namespace}/{self.name}')

# example json:
# {
#   "args":{
#       "i":{
#           "desc":"first Number", "position":0, "required":true, "type":"number"
#       },
#      "j":{
#           "desc":"second Number", "position":1, "required":true, "type":"number"
#      }
#   },
#   "desc": "Sums to numbers",
#   "method_name":"sum",
#   "returns":{
#       "desc":"i + j", "type":"number"
#   }
# }
        # build the docstring.
        docstring = f'{self.name}('
        posnames = [a[0] for a in posargs if a is not None]
        docstring += ", ".join(posnames)
        optnames = [f'{a[0]}={str(a[1].get("default_val", None))}' for a in optargs]
        docstring += ','.join(optnames)
        docstring += ')\n'
        docstring += '\n'
        docstring += f'{self.desc}\n'
        docstring += '\n'
        docstring += 'Parameters:\n'
        for a in posargs:
            if a is not None:
                docstring += f'{a[0]}: {a[1].get("type", "Unknown type")}\n'
                docstring += f'\t{a[1].get("desc", "No description.")}\n'

        for a in optargs:
            docstring += f'{a[0]}: {a[1].get("type", "Unknown type")}, default={str(a[1].get("default_val", None))}\n'
            docstring += f'\t{a[1].get("desc", "No description.")}\n'
        docstring += '\n'
        retname = self.returns.get('name', '')
        if retname:
            retname += ': '

        docstring += f'Returns: {retname} {self.returns.get("type", "Unknown type")}\n'
        docstring += f'\t{self.returns.get("desc", "No description")}\n'
        return docstring

    @property
    def method_name(self) -> str:
        return self.jsondict.get('method_name', self.name)

    @property
    def exe_name(self) -> str:
        return self.jsondict.get('exe_name', self.name)

    @property
    def desc(self) -> str:
        return self.jsondict.get('desc', 'No description')

    @property
    def returns(self) -> AnyDict:
        return self.jsondict.get('returns', {})


class Clippy:
    def __init__(self, clippy_cfg: Optional[AnyDict] = None, cmd_prefix: str = '', loglevel: int = 0):
        self.clippy_cfg = clippy_cfg
        self.cmd_prefix = cmd_prefix.split()
        self.namespaces: List[str] = []
        self.uuid = uuid.uuid4()
        self.logger = logging.getLogger(self.uuid.hex)
        handler = logging.StreamHandler(sys.stderr)
        self.logger.addHandler(handler)
        self.logger.setLevel(config.loglevel)
        self.logger.info(f'Logger set to {self.logger.getEffectiveLevel()}')
        if clippy_cfg is not None:
            self.add_namespaces(clippy_cfg)

    def add_namespaces(self, cmd_dict: AnyDict):
        '''
        Adds namespaces to / replaces namespaces in a current
        Clippy object. Namespaces should be a dictionary
        {'name':'directory'}.

        If a namespace already exists with a given name,
        all methods within that namespace will be replaced
        by the methods from the new directory.
        '''
        j = get_registered_commands(self.logger, cmd_dict)
        for namespace, cmds in j.items():
            if namespace in self.namespaces:  # if the namespace exists, clear it out.
                self.logger.info(f'Replacing namespace {namespace}')
                delattr(self, namespace)
            else:  # this is a new namespace. Let's add it to the list.
                self.logger.debug(f'Adding namespace {namespace}')
                self.namespaces.append(namespace)

            inner = type(namespace, (), {})
            setattr(inner, 'methods', [])
            setattr(inner, 'classes', [])

            for name, jsondict in cmds.items():
                if not inspect.isclass(jsondict):
                    self.logger.debug(f'Adding registered command: {name}')
                    cmd = Command(namespace, name, self, jsondict)
                    setattr(inner, name, types.MethodType(cmd.genfn(cmd.docstring), self))
                    inner.methods.append(name)  # type: ignore
                else:
                    print(f'{name} is a class')  # TODO: is this a debug statement?
                    setattr(inner, name, jsondict)
                    inner.classes.append(name)  # type: ignore
            setattr(self, namespace, inner)

    def logo(self):
        logo()

    def _validate(self, cmd: str, submission_dict: AnyDict) -> Tuple[bool, str]:
        '''
        Internal command.

        Runs the command in dry-run mode only, to validate input.
        Returns True or False if there are warnings (no errors),
        along with any stderr messageas.
        Will throw a ValidationError if there are errors.
        Calls _exec.
        '''
        self.logger.debug(f'Validating {cmd}')
        validate_cmd_prefix = config.validate_cmd_prefix.split()
        p = _exec(validate_cmd_prefix + [cmd, DRY_RUN_FLAG], submission_dict, self.logger)
        if p.returncode:
            raise ClippyValidationError(p.stderr)

        warn = ''
        ret = True
        if p.stderr:
            self.logger.warn(f'Received {p.stderr}')
            ret = False
            warn = p.stderr
        self.logger.debug(f'Validation returning {ret}')
        return (ret, warn)

    def _run(self, cmd: str, submission_dict: AnyDict) -> AnyDict:
        '''
        Processes a submission locally (no remote server).
        Returns a Python dictionary of results, or throws
        a ClippyBackendError if the backend process abended.
        Calls _exec.
        '''

        cmd_prefix = config.cmd_prefix.split()
        self.logger.debug(f'Running {cmd_prefix + [cmd]}')
        p = _exec(cmd_prefix + [cmd], submission_dict, self.logger)
        if p.returncode:
            raise ClippyBackendError(p.stderr)
        self.logger.debug(f'Received stdout: {p.stdout}')
        if p.stderr:
            self.logger.warn(f'Received stderr: {p.stderr}')

        # if we have no output, we still need SOMETHING to feed json.loads, so let's set it to a scalar 'null'.
        output = 'null' if not p.stdout else p.stdout
        # PP: enable functions to return known objects
        # was: return json.loads(output)
        return json.loads(output, object_hook=decode_clippy_json)


def logo():
    print('''
 ╭────────────────────────────────────╮
 │ It looks like you want to use HPC. │
 │ Would you like help with that?     │
 ╰────────────────────────────────────╯
  ╲
   ╲
    ╭──╮
    ⊙ ⊙│╭
    ││ ││
    │╰─╯│
    ╰───╯
''')


logo()
