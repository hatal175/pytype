"""Code for checking and inferring types."""

import collections
import logging
import re
import subprocess
from typing import Any, Dict, Union

from pytype import abstract
from pytype import abstract_utils
from pytype import convert_structural
from pytype import debug
from pytype import function
from pytype import metrics
from pytype import output
from pytype import special_builtins
from pytype import state as frame_state
from pytype import vm
from pytype.overlays import typing_overlay
from pytype.pytd import builtins
from pytype.pytd import escape
from pytype.pytd import optimize
from pytype.pytd import pytd
from pytype.pytd import pytd_utils
from pytype.pytd import visitors
from pytype.typegraph import cfg

log = logging.getLogger(__name__)

# Most interpreter functions (including lambdas) need to be analyzed as
# stand-alone functions. The exceptions are comprehensions and generators, which
# have names like "<listcomp>" and "<genexpr>".
_SKIP_FUNCTION_RE = re.compile("<(?!lambda).+>$")


CallRecord = collections.namedtuple(
    "CallRecord", ["node", "function", "signatures", "positional_arguments",
                   "keyword_arguments", "return_value"])


# How deep to follow call chains:
INIT_MAXIMUM_DEPTH = 4  # during module loading
MAXIMUM_DEPTH = 3  # during non-quick analysis
QUICK_CHECK_MAXIMUM_DEPTH = 2  # during quick checking
QUICK_INFER_MAXIMUM_DEPTH = 1  # during quick inference


class _Initializing:
  pass


class CallTracer(vm.VirtualMachine):
  """Virtual machine that records all function calls.

  Attributes:
    exitpoint: A CFG node representing the program exit. Needs to be set before
      analyze_types.
  """

  _CONSTRUCTORS = ("__new__", "__init__")

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self._unknowns = {}
    self._calls = set()
    self._method_calls = set()
    # Used by init_class.
    self._instance_cache: Dict[Any, Union[_Initializing, cfg.Variable]] = {}
    # Used by call_init. Can differ from _instance_cache because we also call
    # __init__ on classes not initialized via init_class.
    self._initialized_instances = set()
    self._interpreter_functions = []
    self._interpreter_classes = []
    self._analyzed_functions = set()
    self._analyzed_classes = set()
    self._generated_classes = {}
    self.exitpoint = None

  def create_varargs(self, node):
    value = abstract.Instance(self.convert.tuple_type, self)
    value.merge_instance_type_parameter(
        node, abstract_utils.T, self.convert.create_new_unknown(node))
    return value.to_variable(node)

  def create_kwargs(self, node):
    key_type = self.convert.primitive_class_instances[str].to_variable(node)
    value_type = self.convert.create_new_unknown(node)
    kwargs = abstract.Instance(self.convert.dict_type, self)
    kwargs.merge_instance_type_parameter(node, abstract_utils.K, key_type)
    kwargs.merge_instance_type_parameter(node, abstract_utils.V, value_type)
    return kwargs.to_variable(node)

  def create_method_arguments(self, node, method, use_defaults=False):
    """Create arguments for the given method.

    Creates Unknown objects as arguments for the given method. Note that we
    don't need to take parameter annotations into account as
    InterpreterFunction.call() will take care of that.

    Args:
      node: The current node.
      method: An abstract.InterpreterFunction.
      use_defaults: Whether to use parameter defaults for arguments. When True,
        unknown arguments are created with force=False, as it is fine to use
        Unsolvable rather than Unknown objects for type-checking defaults.

    Returns:
      A tuple of a node and a function.Args object.
    """
    args = []
    num_posargs = method.argcount(node)
    num_posargs_no_default = num_posargs - len(method.defaults)
    for i in range(num_posargs):
      default_idx = i - num_posargs_no_default
      if use_defaults and default_idx >= 0:
        arg = method.defaults[default_idx]
      else:
        arg = self.convert.create_new_unknown(node, force=not use_defaults)
      args.append(arg)
    kws = {}
    for key in method.signature.kwonly_params:
      if use_defaults and key in method.kw_defaults:
        kws[key] = method.kw_defaults[key]
      else:
        kws[key] = self.convert.create_new_unknown(node, force=not use_defaults)
    starargs = self.create_varargs(node) if method.has_varargs() else None
    starstarargs = self.create_kwargs(node) if method.has_kwargs() else None
    return node, function.Args(posargs=tuple(args),
                               namedargs=kws,
                               starargs=starargs,
                               starstarargs=starstarargs)

  def call_function_with_args(self, node, val, args):
    """Call a function.

    Args:
      node: The given node.
      val: A cfg.Binding containing the function.
      args: A function.Args object.

    Returns:
      A tuple of (1) a node and (2) a cfg.Variable of the return value.
    """
    fvar = val.AssignToNewVariable(node)
    with val.data.record_calls():
      new_node, ret = self.call_function_in_frame(node, fvar, *args)
    return new_node, ret

  def call_function_in_frame(self, node, var, args, kwargs,
                             starargs, starstarargs):
    frame = frame_state.SimpleFrame(node=node)
    self.push_frame(frame)
    log.info("Analyzing %r", [v.name for v in var.data])
    state = frame_state.FrameState.init(node, self)
    state, ret = self.call_function_with_state(
        state, var, args, kwargs, starargs, starstarargs)
    self.pop_frame(frame)
    return state.node, ret

  def _maybe_fix_classmethod_cls_arg(self, node, cls, func, args):
    sig = func.signature
    if (args.posargs and sig.param_names and
        (sig.param_names[0] not in sig.annotations)):
      # fix "cls" parameter
      return args._replace(
          posargs=(cls.AssignToNewVariable(node),) + args.posargs[1:])
    else:
      return args

  def maybe_analyze_method(self, node, val, cls=None):
    method = val.data
    fname = val.data.name
    if isinstance(method, abstract.INTERPRETER_FUNCTION_TYPES):
      self._analyzed_functions.add(method.get_first_opcode())
      if (not self.options.analyze_annotated and
          (method.signature.has_return_annotation or method.has_overloads) and
          fname.rsplit(".", 1)[-1] not in self._CONSTRUCTORS):
        log.info("%r has annotations, not analyzing further.", fname)
      else:
        for f in method.iter_signature_functions():
          node, args = self.create_method_arguments(node, f)
          if f.is_classmethod and cls:
            args = self._maybe_fix_classmethod_cls_arg(node, cls, f, args)
          node, _ = self.call_function_with_args(node, val, args)
    return node

  def _call_with_fake_args(self, node0, funcv):
    """Attempt to call the given function with made-up arguments."""
    # TODO(tsudol): If expand this beyond __init__, need to handle
    # DictKeyMissing
    nodes = []
    rets = []
    for funcb in funcv.bindings:
      func = funcb.data
      log.info("Trying %s with fake arguments", func)

      if isinstance(func, abstract.INTERPRETER_FUNCTION_TYPES):
        node1, args = self.create_method_arguments(node0, func)
        # Once the args are generated, try calling the function.
        # call_function will check fallback_to_unsolvable if a DictKeyMissing or
        # FailedFunctionCall error is raised when the target function is called.
        # DictKeyMissing doesn't trigger call_with_fake_args, so that shouldn't
        # be raised again, and generating fake arguments should avoid any
        # FailedFunctionCall errors. To prevent an infinite recursion loop, set
        # fallback_to_unsolvable to False just in case.
        # This means any additional errors that may be raised will be passed to
        # the call_function that called this method in the first place.
        node2, ret = self.call_function(node1,
                                        funcb.AssignToNewVariable(),
                                        args,
                                        fallback_to_unsolvable=False)
        nodes.append(node2)
        rets.append(ret)

    if nodes:
      ret = self.join_variables(node0, rets)
      node = self.join_cfg_nodes(nodes)
      if ret.bindings:
        return node, ret
    else:
      node = node0
    log.info("Unable to generate fake arguments for %s", funcv)
    return node, self.new_unsolvable(node)

  def analyze_method_var(self, node0, name, var, cls=None):
    log.info("Analyzing %s", name)
    node1 = node0.ConnectNew(name)
    for val in var.bindings:
      node2 = self.maybe_analyze_method(node1, val, cls)
      node2.ConnectTo(node0)
    return node0

  def bind_method(self, node, name, methodvar, instance_var):
    bound = self.program.NewVariable()
    for m in methodvar.Data(node):
      if isinstance(m, special_builtins.ClassMethodInstance):
        m = m.func.data[0]
        is_cls = True
      else:
        is_cls = (m.isinstance_InterpreterFunction() and m.is_classmethod)
      bound.AddBinding(m.property_get(instance_var, is_cls), [], node)
    return bound

  def _instantiate_binding(self, node0, cls, container):
    """Instantiate a class binding."""
    node1, new = cls.data.get_own_new(node0, cls)
    if not new or (
        any(not isinstance(f, abstract.InterpreterFunction) for f in new.data)):
      # This assumes that any inherited __new__ method defined in a pyi file
      # returns an instance of the current class.
      return node0, cls.data.instantiate(node0, container=container)
    instance = self.program.NewVariable()
    nodes = []
    for b in new.bindings:
      self._analyzed_functions.add(b.data.get_first_opcode())
      node2, args = self.create_method_arguments(node1, b.data)
      args = self._maybe_fix_classmethod_cls_arg(node0, cls, b.data, args)
      node3 = node2.ConnectNew()
      node4, ret = self.call_function_with_args(node3, b, args)
      instance.PasteVariable(ret)
      nodes.append(node4)
    return self.join_cfg_nodes(nodes), instance

  def _instantiate_var(self, node, clsv, container):
    """Build an (dummy) instance from a class, for analyzing it."""
    n = self.program.NewVariable()
    for cls in clsv.Bindings(node, strict=False):
      node, var = self._instantiate_binding(node, cls, container)
      n.PasteVariable(var)
    return node, n

  def _mark_maybe_missing_members(self, values):
    """Set maybe_missing_members to True on these values and their type params.

    Args:
      values: A list of BaseValue objects. On every instance among
        the values, recursively set maybe_missing_members to True on the
        instance and its type parameters.
    """
    values = list(values)
    seen = set()
    while values:
      v = values.pop(0)
      if v not in seen:
        seen.add(v)
        if isinstance(v, abstract.SimpleValue):
          v.maybe_missing_members = True
          for child in v.instance_type_parameters.values():
            values.extend(child.data)

  def init_class(self, node, cls, container=None, extra_key=None):
    """Instantiate a class, and also call __init__.

    Calling __init__ can be expensive, so this method caches its created
    instances. If you don't need __init__ called, use cls.instantiate instead.

    Args:
      node: The current node.
      cls: The class to instantiate.
      container: Optionally, a container to pass to the class's instantiate()
        method, so that type parameters in the container's template are
        instantiated to TypeParameterInstance.
      extra_key: Optionally, extra information about the location at which the
        instantion occurs. By default, this method keys on the current opcode
        and the class, which sometimes isn't enough to disambiguate callers
        that shouldn't get back the same cached instance.

    Returns:
      A tuple of node and instance variable.
    """
    key = (self.frame and self.frame.current_opcode, extra_key, cls)
    instance = self._instance_cache.get(key)
    if not instance or isinstance(instance, _Initializing):
      clsvar = cls.to_variable(node)
      node, instance = self._instantiate_var(node, clsvar, container)
      if key in self._instance_cache:
        # We've encountered a recursive pattern such as
        # class A:
        #   def __init__(self, x: "A"): ...
        # Calling __init__ again would lead to an infinite loop, so
        # we instead create an incomplete instance that will be
        # overwritten later. Note that we have to create a new
        # instance rather than using the one that we're already in
        # the process of initializing - otherwise, setting
        # maybe_missing_members to True would cause pytype to ignore
        # all attribute errors on self in __init__.
        self._mark_maybe_missing_members(instance.data)
      else:
        self._instance_cache[key] = _Initializing()
        node = self.call_init(node, instance)
      self._instance_cache[key] = instance
    return node, instance

  def _call_method(self, node, binding, method_name):
    node, method = self.attribute_handler.get_attribute(
        node, binding.data.get_class(), method_name, binding)
    if method:
      bound_method = self.bind_method(
          node, method_name, method, binding.AssignToNewVariable())
      node = self.analyze_method_var(node, method_name, bound_method)
    return node

  def _call_init_on_binding(self, node, b):
    if isinstance(b.data, abstract.SimpleValue):
      for param in b.data.instance_type_parameters.values():
        node = self.call_init(node, param)
    node = self._call_method(node, b, "__init__")
    cls = b.data.get_class()
    if isinstance(cls, abstract.InterpreterClass):
      # Call any additional initalizers the class has registered.
      for method in cls.additional_init_methods:
        node = self._call_method(node, b, method)
    return node

  def call_init(self, node, instance):
    # Call __init__ on each binding.
    for b in instance.bindings:
      if b.data in self._initialized_instances:
        continue
      self._initialized_instances.add(b.data)
      node = self._call_init_on_binding(node, b)
    return node

  def reinitialize_if_initialized(self, node, instance):
    if instance in self._initialized_instances:
      self._call_init_on_binding(node, instance.to_binding(node))

  def analyze_class(self, node, val):
    self._analyzed_classes.add(val.data)
    node, instance = self.init_class(node, val.data)
    good_instances = [b for b in instance.bindings if val.data == b.data.cls]
    if not good_instances:
      # __new__ returned something that's not an instance of our class.
      instance = val.data.instantiate(node)
      node = self.call_init(node, instance)
    elif len(good_instances) != len(instance.bindings):
      # __new__ returned some extra possibilities we don't need.
      instance = self.join_bindings(node, good_instances)
    for instance_value in instance.data:
      val.data.register_canonical_instance(instance_value)
    for name, methodvar in sorted(val.data.members.items()):
      if name in self._CONSTRUCTORS:
        continue  # We already called this method during initialization.
      b = self.bind_method(node, name, methodvar, instance)
      node = self.analyze_method_var(node, name, b, val)
    return node

  def analyze_function(self, node0, val):
    if val.data.is_attribute_of_class:
      # We'll analyze this function as part of a class.
      log.info("Analyze functions: Skipping class method %s", val.data.name)
    else:
      node1 = node0.ConnectNew(val.data.name)
      node2 = self.maybe_analyze_method(node1, val)
      node2.ConnectTo(node0)
    return node0

  def _should_analyze_as_interpreter_function(self, data):
    # We record analyzed functions by opcode rather than function object. The
    # two ways of recording are equivalent except for closures, which are
    # re-generated when the variables they close over change, but we don't want
    # to re-analyze them.
    return (isinstance(data, abstract.InterpreterFunction) and
            not data.is_overload and
            not data.is_class_builder and
            data.get_first_opcode() not in self._analyzed_functions and
            not _SKIP_FUNCTION_RE.search(data.name))

  def analyze_toplevel(self, node, defs):
    for name, var in sorted(defs.items()):  # sort, for determinicity
      if not self._is_typing_member(name, var):
        for value in var.bindings:
          if isinstance(value.data, abstract.InterpreterClass):
            new_node = self.analyze_class(node, value)
          elif (isinstance(value.data, abstract.INTERPRETER_FUNCTION_TYPES) and
                not value.data.is_overload):
            new_node = self.analyze_function(node, value)
          else:
            continue
          if new_node is not node:
            new_node.ConnectTo(node)
    # Now go through all functions and classes we haven't analyzed yet.
    # These are typically hidden under a decorator.
    # Go through classes first so that the `is_attribute_of_class` will
    # be set for all functions in class.
    for c in self._interpreter_classes:
      for value in c.bindings:
        if (isinstance(value.data, abstract.InterpreterClass) and
            value.data not in self._analyzed_classes):
          node = self.analyze_class(node, value)
    for f in self._interpreter_functions:
      for value in f.bindings:
        if self._should_analyze_as_interpreter_function(value.data):
          node = self.analyze_function(node, value)
    return node

  def analyze(self, node, defs, maximum_depth):
    assert not self.frame
    self.maximum_depth = maximum_depth
    self._analyzing = True
    node = node.ConnectNew(name="Analyze")
    return self.analyze_toplevel(node, defs)

  def trace_unknown(self, name, unknown_binding):
    self._unknowns[name] = unknown_binding

  def trace_call(self, node, func, sigs, posargs, namedargs, result):
    """Add an entry into the call trace.

    Args:
      node: The CFG node right after this function call.
      func: A cfg.Binding of a function that was called.
      sigs: The signatures that the function might have been called with.
      posargs: The positional arguments, an iterable over cfg.Value.
      namedargs: The keyword arguments, a dict mapping str to cfg.Value.
      result: A Variable of the possible result values.
    """
    log.debug("Logging call to %r with %d args, return %r",
              func, len(posargs), result)
    args = tuple(posargs)
    kwargs = tuple((namedargs or {}).items())
    record = CallRecord(node, func, sigs, args, kwargs, result)
    if isinstance(func.data, abstract.BoundPyTDFunction):
      self._method_calls.add(record)
    elif isinstance(func.data, abstract.PyTDFunction):
      self._calls.add(record)

  def trace_functiondef(self, f):
    self._interpreter_functions.append(f)

  def trace_classdef(self, c):
    self._interpreter_classes.append(c)

  def trace_namedtuple(self, nt):
    # All namedtuple instances with the same name are equal, so it's fine to
    # overwrite previous instances.
    self._generated_classes[nt.name] = nt

  def pytd_classes_for_unknowns(self):
    classes = []
    for name, val in self._unknowns.items():
      if val in val.variable.Filter(self.exitpoint, strict=False):
        classes.append(val.data.to_structural_def(self.exitpoint, name))
    return classes

  def pytd_for_types(self, defs):
    # If a variable is annotated, we'll always output that type.
    annotated_names = set()
    data = []
    pytd_convert = self.convert.pytd_convert
    annots = abstract_utils.get_annotations_dict(defs)
    for name, t in pytd_convert.annotations_to_instance_types(
        self.exitpoint, annots):
      annotated_names.add(name)
      data.append(pytd.Constant(name, t))
    for name, var in defs.items():
      if (name in output.TOP_LEVEL_IGNORE or name in annotated_names or
          self._is_typing_member(name, var)):
        continue
      options = var.FilteredData(self.exitpoint, strict=False)
      if (len(options) > 1 and
          not all(isinstance(o, abstract.FUNCTION_TYPES) for o in options)):
        # It's ambiguous whether this is a type, a function or something
        # else, so encode it as a constant.
        combined_types = pytd_utils.JoinTypes(t.to_type(self.exitpoint)
                                              for t in options)
        data.append(pytd.Constant(name, combined_types))
      elif options:
        for option in options:
          try:
            d = option.to_pytd_def(self.exitpoint, name)  # Deep definition
          except NotImplementedError:
            d = option.to_type(self.exitpoint)  # Type only
            if isinstance(d, pytd.NothingType):
              if isinstance(option, abstract.Empty):
                d = pytd.AnythingType()
              else:
                assert isinstance(option, typing_overlay.NoReturn)
          if isinstance(d, pytd.Type) and not isinstance(d, pytd.TypeParameter):
            data.append(pytd.Constant(name, d))
          else:
            data.append(d)
      else:
        log.error("No visible options for %s", name)
        data.append(pytd.Constant(name, pytd.AnythingType()))
    return pytd_utils.WrapTypeDeclUnit("inferred", data)

  @staticmethod
  def _call_traces_to_function(call_traces, name_transform=lambda x: x):
    funcs = collections.defaultdict(pytd_utils.OrderedSet)
    for node, func, sigs, args, kws, retvar in call_traces:
      # The lengths may be different in the presence of optional and kw args.
      arg_names = max((sig.get_positional_names() for sig in sigs), key=len)
      for i in range(len(arg_names)):
        if not isinstance(func.data, abstract.BoundFunction) or i > 0:
          arg_names[i] = function.argname(i)
      arg_types = (a.data.to_type(node) for a in args)
      ret = pytd_utils.JoinTypes(t.to_type(node) for t in retvar.data)
      starargs = None
      starstarargs = None
      funcs[func.data.name].add(pytd.Signature(
          tuple(pytd.Parameter(n, t, False, False, None)
                for n, t in zip(arg_names, arg_types)) +
          tuple(pytd.Parameter(name, a.data.to_type(node), False, False, None)
                for name, a in kws),
          starargs, starstarargs,
          ret, exceptions=(), template=()))
    functions = []
    for name, signatures in funcs.items():
      functions.append(pytd.Function(name_transform(name), tuple(signatures),
                                     pytd.MethodTypes.METHOD))
    return functions

  def _is_typing_member(self, name, var):
    for module_name in ("typing", "typing_extensions"):
      if module_name not in self.loaded_overlays:
        continue
      module = self.loaded_overlays[module_name].get_module(name)
      if name in module.members and module.members[name].data == var.data:
        return True
    return False

  def pytd_functions_for_call_traces(self):
    return self._call_traces_to_function(self._calls, escape.pack_partial)

  def pytd_classes_for_call_traces(self):
    class_to_records = collections.defaultdict(list)
    for call_record in self._method_calls:
      args = call_record.positional_arguments
      if not any(isinstance(a.data, abstract.Unknown) for a in args):
        # We don't need to record call signatures that don't involve
        # unknowns - there's nothing to solve for.
        continue
      cls = args[0].data.get_class()
      if isinstance(cls, abstract.PyTDClass):
        class_to_records[cls].append(call_record)
    classes = []
    for cls, call_records in class_to_records.items():
      full_name = cls.module + "." + cls.name if cls.module else cls.name
      classes.append(pytd.Class(
          name=escape.pack_partial(full_name),
          metaclass=None,
          parents=(pytd.NamedType("builtins.object"),),  # not used in solver
          methods=tuple(self._call_traces_to_function(call_records)),
          constants=(),
          classes=(),
          decorators=(),
          slots=None,
          template=(),
      ))
    return classes

  def pytd_classes_for_namedtuple_instances(self):
    return tuple(v.generate_ast() for v in self._generated_classes.values())

  def compute_types(self, defs):
    classes = (tuple(self.pytd_classes_for_unknowns()) +
               tuple(self.pytd_classes_for_call_traces()) +
               self.pytd_classes_for_namedtuple_instances())
    functions = tuple(self.pytd_functions_for_call_traces())
    aliases = ()  # aliases are instead recorded as constants
    ty = pytd_utils.Concat(
        self.pytd_for_types(defs),
        pytd_utils.CreateModule("unknowns", classes=classes,
                                functions=functions, aliases=aliases))
    ty = ty.Visit(optimize.CombineReturnsAndExceptions())
    ty = ty.Visit(optimize.PullInMethodClasses())
    ty = ty.Visit(visitors.DefaceUnresolved(
        [ty, self.loader.concat_all()], escape.UNKNOWN))
    return ty.Visit(visitors.AdjustTypeParameters())

  def _check_return(self, node, actual, formal):
    if not self.options.report_errors:
      return True
    views = abstract_utils.get_views([actual], node)
    # Check for typevars in the return value first, since bad_matches
    # expects not to get any.
    bad = [view for view in views
           if actual in view and view[actual].data.formal]
    if not bad:
      bad = self.matcher.bad_matches(actual, formal, node)
    if bad:
      self.errorlog.bad_return_type(
          self.frames, node, formal, actual, bad)
    return not bad


def check_types(src, filename, errorlog, options, loader,
                deep=True, init_maximum_depth=INIT_MAXIMUM_DEPTH,
                maximum_depth=None, **kwargs):
  """Verify the Python code."""
  tracer = CallTracer(errorlog=errorlog, options=options,
                      generate_unknowns=False, loader=loader, **kwargs)
  loc, defs = tracer.run_program(src, filename, init_maximum_depth)
  snapshotter = metrics.get_metric("memory", metrics.Snapshot)
  snapshotter.take_snapshot("analyze:check_types:tracer")
  if deep:
    if maximum_depth is None:
      maximum_depth = (
          QUICK_CHECK_MAXIMUM_DEPTH if options.quick else MAXIMUM_DEPTH)
    tracer.analyze(loc, defs, maximum_depth=maximum_depth)
  snapshotter.take_snapshot("analyze:check_types:post")
  _maybe_output_debug(options, tracer.program)


def infer_types(src, errorlog, options, loader,
                filename=None, deep=True, init_maximum_depth=INIT_MAXIMUM_DEPTH,
                show_library_calls=False, maximum_depth=None, tracer_vm=None,
                **kwargs):
  """Given Python source return its types.

  Args:
    src: A string containing Python source code.
    errorlog: Where error messages go. Instance of errors.ErrorLog.
    options: config.Options object
    loader: A load_pytd.Loader instance to load PYI information.
    filename: Filename of the program we're parsing.
    deep: If True, analyze all functions, even the ones not called by the main
      execution flow.
    init_maximum_depth: Depth of analysis during module loading.
    show_library_calls: If True, call traces are kept in the output.
    maximum_depth: Depth of the analysis. Default: unlimited.
    tracer_vm: An instance of CallTracer, in case the caller wants to
      instantiate and retain the vm used for type inference.
    **kwargs: Additional parameters to pass to vm.VirtualMachine
  Returns:
    A tuple of (ast: TypeDeclUnit, builtins: TypeDeclUnit)
  Raises:
    AssertionError: In case of a bad parameter combination.
  """
  # If the caller has passed in a vm, use that.
  if tracer_vm:
    assert isinstance(tracer_vm, CallTracer)
    tracer = tracer_vm
  else:
    tracer = CallTracer(errorlog=errorlog, options=options,
                        generate_unknowns=options.protocols,
                        store_all_calls=not deep, loader=loader, **kwargs)
  loc, defs = tracer.run_program(src, filename, init_maximum_depth)
  log.info("===Done running definitions and module-level code===")
  snapshotter = metrics.get_metric("memory", metrics.Snapshot)
  snapshotter.take_snapshot("analyze:infer_types:tracer")
  if deep:
    if maximum_depth is None:
      if not options.quick:
        maximum_depth = MAXIMUM_DEPTH
      elif options.analyze_annotated:
        # Since there's no point in analyzing annotated functions for inference,
        # the presence of this option means that the user wants checking, too.
        maximum_depth = QUICK_CHECK_MAXIMUM_DEPTH
      else:
        maximum_depth = QUICK_INFER_MAXIMUM_DEPTH
    tracer.exitpoint = tracer.analyze(loc, defs, maximum_depth)
  else:
    tracer.exitpoint = loc
  snapshotter.take_snapshot("analyze:infer_types:post")
  ast = tracer.compute_types(defs)
  ast = tracer.loader.resolve_ast(ast)
  if tracer.has_unknown_wildcard_imports or any(
      a in defs for a in abstract_utils.DYNAMIC_ATTRIBUTE_MARKERS):
    if "__getattr__" not in ast:
      ast = pytd_utils.Concat(
          ast, builtins.GetDefaultAst(options.python_version))
  # If merged with other if statement, triggers a ValueError: Unresolved class
  # when attempts to load from the protocols file
  if options.protocols:
    protocols_pytd = tracer.loader.import_name("protocols")
  else:
    protocols_pytd = None
  builtins_pytd = tracer.loader.concat_all()
  # Insert type parameters, where appropriate
  ast = ast.Visit(visitors.CreateTypeParametersForSignatures())
  if options.protocols:
    log.info("=========== PyTD to solve =============\n%s",
             pytd_utils.Print(ast))
    ast = convert_structural.convert_pytd(ast, builtins_pytd, protocols_pytd)
  elif not show_library_calls:
    log.info("Solving is turned off. Discarding call traces.")
    # Rename remaining "~unknown" to "?"
    ast = ast.Visit(visitors.RemoveUnknownClasses())
    # Remove "~list" etc.:
    ast = convert_structural.extract_local(ast)
  _maybe_output_debug(options, tracer.program)
  return ast, builtins_pytd


def _maybe_output_debug(options, program):
  """Maybe emit debugging output."""
  if options.output_cfg or options.output_typegraph:
    dot = debug.program_to_dot(program, set([]), bool(options.output_cfg))
    svg_file = options.output_cfg or options.output_typegraph
    proc = subprocess.Popen(["/usr/bin/dot", "-T", "svg", "-o", svg_file],
                            stdin=subprocess.PIPE, universal_newlines=True)
    (_, stderr) = proc.communicate(dot)
    if stderr:
      log.info("Failed to create %s: %s", svg_file, stderr)
  if options.output_debug:
    text = debug.program_to_text(program)
    if options.output_debug == "-":
      log.info("=========== Program Dump =============\n%s", text)
    else:
      with options.open_function(options.output_debug, "w") as fi:
        fi.write(text)
