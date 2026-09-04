"""The TargetSmart voter-file importer package.

Convention: each importer package defines its `Importer` in `importer.py`;
supporting stages (`transform`, `manifest`) live in sibling modules. This
`__init__` just re-exports the importer class.
"""

from src.importers.targetsmart.importer import TargetSmartImporter

__all__ = ["TargetSmartImporter"]
