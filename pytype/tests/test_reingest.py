"""Tests for reloading generated pyi."""

import unittest


from pytype import utils
from pytype.pytd import pytd
from pytype.tests import test_inference


class ReingestTest(test_inference.InferenceTest):
  """Tests for reloading the pyi we generate."""

  def testContainer(self):
    ty = self.Infer("""
      class Container:
        def Add(self):
          pass
      class A(Container):
        pass
    """)
    with utils.Tempdir() as d:
      d.create_file("foo.pyi", pytd.Print(ty))
      self.assertNoErrors("""
        # u.py
        from foo import A
        A().Add()
      """, pythonpath=[d.path])

  def testUnion(self):
    ty = self.Infer("""
      class Union(object):
        pass
      x = {"Union": Union}
    """)
    with utils.Tempdir() as d:
      d.create_file("foo.pyi", pytd.Print(ty))
      self.assertNoErrors("""
        from foo import Union
      """, pythonpath=[d.path])

  def testIdentityDecorators(self):
    foo = self.Infer("""
      def decorate(f):
        return f
    """, deep=True)
    with utils.Tempdir() as d:
      d.create_file("foo.pyi", pytd.Print(foo))
      ty = self.Infer("""
        import foo
        @foo.decorate
        def f():
          return 3
        def g():
          return f()
      """, deep=True, pythonpath=[d.path])
      self.assertTypesMatchPytd(ty, """
        foo = ...  # type: module
        def f() -> int
        def g() -> int
      """)

  @unittest.skip("Needs better handling of Union[Callable, f] in output.py.""")
  def testMaybeIdentityDecorators(self):
    foo = self.Infer("""
      def maybe_decorate(f):
        return f or (lambda *args: 42)
    """, deep=True)
    with utils.Tempdir() as d:
      d.create_file("foo.pyi", pytd.Print(foo))
      ty = self.Infer("""
        import foo
        @foo.maybe_decorate
        def f():
          return 3
        def g():
          return f()
      """, deep=True, pythonpath=[d.path])
      self.assertTypesMatchPytd(ty, """
        foo = ...  # type: module
        def f() -> int
        def g() -> int
      """)

  def testTypeParameterBound(self):
    foo = self.Infer("""
      from __future__ import google_type_annotations
      from typing import TypeVar
      T = TypeVar("T", bound=float)
      def f(x: T) -> T: return x
    """)
    with utils.Tempdir() as d:
      d.create_file("foo.pyi", pytd.Print(foo))
      _, errors = self.InferAndCheck("""\
        import foo
        foo.f("")
      """, pythonpath=[d.path])
      self.assertErrorLogIs(errors, [(2, "wrong-arg-types", r"float.*str")])

  def testNamedTuple(self):
    foo = self.Infer("""
      import collections
      X = collections.namedtuple("X", ["a", "b"])
    """)
    with utils.Tempdir() as d:
      d.create_file("foo.pyi", pytd.Print(foo))
      self.assertNoErrors("""
        import foo
        foo.X(0, 0)
        foo.X(a=0, b=0)
      """, pythonpath=[d.path])


if __name__ == "__main__":
  test_inference.main()
