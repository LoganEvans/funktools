#!/usr/bin/env python3
"""Provides `CLI` decorator class and sane-default instantiated `cli` decorator instance.

The decorator may be used to simplify generation of a CLI based entirely on decorated entrypoint function signature.

Single-entrypoint example:

    - file: foo.py
        from atools.cli import CLI


        @CLI()  # This will add `.cli` decoration to `entrypoint`.
        def entrypoint(a: int, /, b: str, c: bool = True, *, d: float, e: tuple = tuple()) -> ...:
            ...


        if __name__ == '__main__':
            # This will parse `sys.argv[1:]` and run entrypoint with parsed arguments.
            entrypoint.cli.run()

    - Command line executions:
        $ ./foo.py 1 "this is b" --d 0.1"
        $ ./foo.py 1 "this is b" --no-c --d 0.1 --e "t0" "t1" "t2"

Multiple-entrypoint example:

    - file: prog/__init__.py
        import atools


        @atools.CLI(submodules=True)  # This will find entrypoints in submodules named `entrypoint`.
        def entrypoint(a: int, /, b: str, c: bool = True, *, d: float, e: tuple = tuple()) -> ...:
            ...

    - file: prog/foo.py
        def entrypoint

    - file: __main__.py
        if __name__ == '__main__':
            # This will parse `sys.argv[1:]` and run entrypoint with parsed arguments.
            entrypoint.cli.run()

"""
from __future__ import annotations
import argparse
import ast
import asyncio
import builtins
import dataclasses
import inspect
import importlib
import pkgutil
import types
import typing
import sys


@dataclasses.dataclass(frozen=True)
class _Decoration[** Params, Return]:
    """CLI decoration attached to decorated entrypoint at `<entrypoint>.cli`.

    A _Decoration instance is attached to an entrypoint decorated via _Decorator.__call__. The `run` function can then
    be called with `<entrypoint>.cli.run`.
    """
    _: dataclasses.KW_ONLY
    _parser: argparse.ArgumentParser

    def run(self, args: list[str] = ...) -> object:
        """Parses args, runs parser's registered entrypoint with parsed args, and return the result.

        Note that the entrypoint that is run is determined by the parser. It may be an entrypoint in a submodule of the
        decorated entrypoint, not the decorated entrypoint.
        
        If the decorated function is a couroutinefunction, it will be run via `asyncio.run`.

        Args (Positional or Keyword):
            args (default: sys.argv[1]): Arguments to be parsed and passed to parser's registered entrypoint.

        Returns:
            object: Result of executing registered entrypoint with given args.
        """
        args = sys.argv[1:] if args is ... else args

        parsed_args = vars(self._parser.parse_args(args))

        # Note that this may be the registered entrypoint of a submodule, not the entrypoint that is decorated.
        entrypoint = parsed_args['entrypoint']
        args, kwargs = [], {}
        for parameter in inspect.signature(entrypoint).parameters.values():
            if parameter.kind == parameter.POSITIONAL_ONLY:
                args.append(parsed_args[parameter.name])
            else:
                kwargs[parameter.name] = parsed_args[parameter.name]

        result = entrypoint(*args, **kwargs)
        if inspect.iscoroutinefunction(entrypoint):
            result = asyncio.run(result)

        return result


class _Entrypoint[** Params, Return](typing.Protocol):
    __call__: typing.Callable[Params, Return]


class _Decorated[** Params, Return](typing.Protocol):
    __call__: typing.Callable[Params, Return]
    cli: _Decoration[Params, Return]


@dataclasses.dataclass(frozen=True)
class _Decorator[** Params, Return]:
    """Decorate a function, adding `<decorated_function>.cli.run` function.

    The `.cli.run` function parses command line arguments (e.g. `sys.argv[1:]`) and executes the decorated function with
    the parsed arguments.

    When created, setting `submodules` to True indicates that the decorator should create a hierarchical parser with
    subcommand structure corresponding to submodule structure starting with the decorated function's module. Any module
    with a function name matching given `entrypoint` name have a corresponding CLI subcommand generated with an
    equivalent CLI signature.

    Parser subcommand documentation is generated from corresponding module docstrings.

    Given a program with the following file structure (column 1), python entrypoints (column 2), the generated CLI
    signature follows (column 3).

              1. Structure          2. entrypoint signature             3. generated CLI signature
        (a)   |- __main__.py                                            prog [-h] {.|foo|baz|quux}
              |- prog
        (b)      |- __init__.py     entrypoint()                        prog . [-h]
                 |- foo.py          entrypoint(pos: int, /)             prog foo [-h] POS
        (a)      |- _bar.py         entrypoint(pos: int = 42, /)        prog _bar [-h] [POS]
                 |- baz
        (c)      |  |- __init__.py  entrypoint(pos_or_kwd: str)         prog baz . [-h] --pos-or-kwd POS_OR_KWD
                 |  |- qux.py       entrypoint(pos_or_kwd: str = 'hi')  prog baz qux [-h] [--pos-or-kwd POS_OR_KWD]
                 |- quux
        (d)         |- __init__.py  entrypoint(*args: list)             Decoration fails with RuntimeError!
        (d)         |- corge.py     entrypoint(**kwargs: dict)          Decoration fails with RuntimeError!

    Note for the diagram above:
        (a) Subcommands that start with underscores are hidden in the CLI signature. They are, however, valid.
        (b) The only `entrypoint` that needs to be decorated is in the toplevel __init__.py.
        (c) Entrypoints in an __init__.py correspond to a `.` CLI subcommand.
        (d) Variadic args and kwargs are unsupported.

    Args (Keyword):
        submodules: If True, subcommands are generated for every submodule in the module hierarchy. CLI bindings are
            generated for each submodule top-level function with name matching decorated entrypoint name.
    """
    _: dataclasses.KW_ONLY
    submodules: bool = False

    _container_types: typing.ClassVar[frozenset[type]] = frozenset({
        builtins.dict,
        builtins.frozenset,
        builtins.list,
        builtins.set,
        builtins.tuple,
    })
    _primitive_types: typing.ClassVar[frozenset[type]] = frozenset({
        builtins.bool,
        builtins.float,
        builtins.int,
        builtins.str,
    })

    _types: typing.ClassVar[frozenset[type]] = _container_types | _primitive_types

    @classmethod
    def _set_t[T](cls, value: object, t: type[T]) -> T:
        match type(value), typing.get_origin(t) or t, typing.get_args(t):
            # Primitive types.
            case builtins.bool, builtins.bool, ():
                value: bool
                value: T = value
            case builtins.float, builtins.float, ():
                value: float
                value: T = value
            case builtins.int, builtins.int, ():
                value: int
                value: T = value
            case builtins.str, builtins.str, ():
                value: str
                value: T = value
            # None and types.NoneType are used interchangeably in Python typing.
            case (None | types.NoneType), (None | types.NoneType), ():
                value: None
                value: T = value

            # Union type.
            # TODO(cevans87): types.UnionType and typing.Union are not equivalent (this is a bug). Once it is fixed, we
            #  can use just types.UnionType. ref. https://github.com/python/cpython/issues/105499.
            case V, (types.UnionType | typing.Union), As if V in As:
                value: V
                value: T = value

            # Container types.
            case builtins.dict, builtins.dict, (A0, A1):
                value: dict
                value: T = {cls._set_t(key, A0): cls._set_t(sub_value, A1) for key, sub_value in value.items()}
            case builtins.set, builtins.frozenset, (A,):
                value: set
                value: T = frozenset({cls._set_t(sub_value, A) for sub_value in value})
            case builtins.list, builtins.list, (A,):
                value: list
                value: T = [cls._set_t(value, A) for value in value]
            case builtins.set, builtins.set, (A,):
                value: set
                value: T = {cls._set_t(value, A) for value in value}
            case builtins.tuple, builtins.tuple, (A0, builtins.Ellipsis):
                value: tuple
                value: T = tuple([cls._set_t(sub_value, A0) for sub_value in value])
            case builtins.tuple, builtins.tuple, (A0, *As):
                value: tuple
                value: T = tuple([cls._set_t(value[0], A0), *cls._set_t(value[1:], tuple[*As])])
            case builtins.tuple, builtins.tuple, ():
                value: tuple[()]
                value: T = tuple()

            # Custom type.
            case V, T, _ if V in cls._types and T not in cls._types:
                value: V
                value: T = T(value)
            case _:
                raise RuntimeError(f'Given {t=} could not be enforced on {value=}.')

        return value

    @classmethod
    def _set_parameter(cls, *, parser: argparse.ArgumentParser, parameter: inspect.Parameter) -> None:
        match parameter.kind:
            case parameter.POSITIONAL_ONLY:
                flag = parser.add_argument(parameter.name)
            case parameter.POSITIONAL_OR_KEYWORD if parameter.default == parameter.empty:
                flag = parser.add_argument(parameter.name)
            case parameter.POSITIONAL_OR_KEYWORD if parameter.default != parameter.empty:
                flag = parser.add_argument(f'--{parameter.name.replace('_', '-')}')
            case parameter.KEYWORD_ONLY:
                flag = parser.add_argument(f'--{parameter.name.replace('_', '-')}')
            case _:
                raise RuntimeError(f'During parser setup: {parameter.name=} has unsupported {parameter.kind=}.')

        t = parameter.annotation
        help_parts = []
        if typing.get_origin(t) is typing.Annotated:
            args = typing.get_args(t)
            t = args[0]
            help_parts.append(args[1])
        if parameter.default == parameter.empty:
            flag.required = True
        else:
            flag.default = parameter.default
            help_parts.append(f'Default: {parameter.default}')
            flag.nargs = argparse.OPTIONAL

        flag.help = ' '.join(help_parts)

        flag.type = lambda value: cls._set_t(value=value if t is str else ast.literal_eval(value), t=t)

    def _set_entrypoint[** SubParams, SubReturn](
        self, *, parser, entrypoint: _Entrypoint[SubParams, SubReturn]
    ) -> None:
        parser.set_defaults(entrypoint=entrypoint)

        for parameter in inspect.signature(entrypoint).parameters.values():
            self._set_parameter(parser=parser, parameter=parameter)

    def __call__(self, entrypoint: _Entrypoint[Params, Return], /) -> _Decorated[Params, Return]:
        module = inspect.getmodule(entrypoint)

        parser = argparse.ArgumentParser(description=entrypoint.__doc__ or module.__doc__)
        parser.set_defaults(entrypoint=parser.print_help)

        entrypoint.cli = _Decoration(_parser=parser)
        decorated: _Decorated[Params, Return] = entrypoint  # type: ignore

        stack = [(parser, module, entrypoint)]

        while stack:
            parser, module, entrypoint = stack.pop()

            if self.submodules and (module.__name__ == module.__package__):
                # This is a package. Add its subpackages to the stack to be also be evaluated.
                subparsers = parser.add_subparsers(title='subcommands', metavar='{subcommand}')
                for _, name, _ in pkgutil.iter_modules(path=module.__path__):
                    sub_module = importlib.import_module(f'{module.__package__}.{name}')
                    if name.startswith('_'):
                        sub_parser = subparsers.add_parser(description=sub_module.__doc__, name=name)
                    else:
                        sub_parser = subparsers.add_parser(description=sub_module.__doc__, help='', name=name)
                    sub_parser.set_defaults(entrypoint=sub_parser.print_help)
                    sub_entrypoint = getattr(sub_module, decorated.__name__, None)
                    stack.append((sub_parser, sub_module, sub_entrypoint))

                if entrypoint is not None:
                    parser = subparsers.add_parser(name='.', help=entrypoint.__doc__ or module.__doc__)

            if entrypoint is not None:
                self._set_entrypoint(parser=parser, entrypoint=entrypoint)

        return decorated


CLI = _Decorator
cli = CLI()
