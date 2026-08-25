"""
The component type registry.

Adding a type is: write the class, add one line to TYPES. The panel's routes,
templates and forms are driven by `fields()`, `tabs()` and `actions()`, and the
CLI is driven by the same three, so neither of them learns the new name.

    from components import load, create, all_components
"""

from . import store
from .app import AppComponent
from .base import Component, Field
from .mongo import MongoComponent
from .redis import RedisComponent
from .store import ComponentError

TYPES = {
    AppComponent.TYPE: AppComponent,
    RedisComponent.TYPE: RedisComponent,
    MongoComponent.TYPE: MongoComponent,
}

__all__ = [
    "Component", "ComponentError", "Field", "TYPES",
    "type_for", "load", "create", "all_components", "names", "exists", "store",
    "groups", "types_in_group", "GROUP_ORDER",
]


#: The "+ New" menu: group label -> the types that create one. Ordered, so the
#: menu reads Application then Database rather than in dict order.
GROUP_ORDER = ["Application", "Database"]


def groups():
    out = {}
    for key, cls in TYPES.items():
        out.setdefault(cls.GROUP, []).append((key, cls))
    return [(g, out[g]) for g in GROUP_ORDER if g in out] + \
           [(g, t) for g, t in out.items() if g not in GROUP_ORDER]


def types_in_group(group):
    return [(key, cls) for key, cls in TYPES.items() if cls.GROUP == group]


def type_for(type_name):
    try:
        return TYPES[type_name]
    except KeyError:
        known = ", ".join(sorted(TYPES))
        raise ComponentError(f"Unknown component type {type_name!r}. Known types: {known}.") from None


def load(name):
    """Read a component from disk. Raises ComponentError if it is not there."""
    data = store.read_spec(name)
    return type_for(data["type"])(name, data)


def exists(name):
    return store.exists(name)


def names():
    return store.list_names()


def all_components():
    """
    Every component on disk, newest type-order first.

    A spec that fails to load does not take the list down with it: an unreadable
    or unknown component is reported as a problem against its name, because a
    panel that shows nothing at all is worse than one that shows nine
    components and an error.
    """
    out, problems = [], []
    for name in names():
        try:
            out.append(load(name))
        except ComponentError as exc:
            problems.append((name, str(exc)))
    out.sort(key=lambda c: (c.CATEGORY, c.name))
    return out, problems


def create(type_name, name, raw_spec):
    """
    Validate and write a new component. Returns (component, problems).

    Nothing is written when there are problems, and nothing is deployed here —
    creating and deploying are separate on purpose, so a mistyped image is a
    form error rather than a stack that half exists.
    """
    cls = type_for(type_name)
    store.check_name(name)
    if store.exists(name):
        raise ComponentError(f"A component named {name!r} already exists.")

    spec, problems = cls.coerce_spec(raw_spec)
    component = cls(name, {"type": type_name, "spec": spec})
    problems += component.validate()
    if problems:
        return None, problems

    # Credentials come from the same form, and a blank one means "generate".
    # Checked BEFORE anything is written, so a password that is too short is a
    # form error rather than a half-created component.
    secret_problems = []
    for secret in type(component).SECRETS:
        supplied = (raw_spec.get(secret.key) or raw_spec.get(secret.key.lower()) or "").strip()
        problem = secret.check(supplied)
        if problem:
            secret_problems.append(problem)
    if secret_problems:
        return None, secret_problems

    store.write_spec(name, component.as_dict())
    # An application starts with an empty environment file so the editor has
    # something to open.
    if isinstance(component, AppComponent):
        store.write_env(name, [], header=[f"# Environment for {name}.", ""])
    component.apply_secrets(raw_spec)
    component.created_at = store.read_spec(name).get("created_at")
    return component, []


def update(name, raw_spec):
    """Re-validate and rewrite an existing component's spec."""
    component = load(name)
    merged = dict(component.spec)
    spec, problems = type(component).coerce_spec({**merged, **raw_spec})
    candidate = type(component)(name, {"type": component.TYPE, "spec": spec,
                                       "created_at": component.created_at})
    problems += candidate.validate()
    if problems:
        return None, problems
    store.write_spec(name, candidate.as_dict())
    return candidate, []
