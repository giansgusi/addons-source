addons-source

Source code for Third-party plugins for Gramps genealogy program

Use `make.py` for Gramps addons.

Examples:
* Creates the initial directories for the addon.
```
python make.py init AddonDirectory
```

* Creates the initial empty `AddonDirectory/po/fr-local.po` file for the addon.
```
python make.py init AddonDirectory fr
```

* Updates `AddonDirectory/po/fr-local.po` with the latest translations.
```
python make.py update AddonDirectory fr
```

* Build `../download/AddonDirectory.addon.tgz`
```
python make.py build AddonDirectory
```

* Build `../download/*.addon.tgz`
```
python make.py build all
```

* Compile the named Addon Directory?
```
python make.py compile AddonDirectory
```

* Compiles `AddonDirectory/po/*-local.po` and puts the resulting `.mo` file in `AddonDirectory/locale/*/LC_MESSAGES/addon.mo`
```
python make.py compile all
```

```
python make.py listing AddonDirectory
```

```
python make.py listing all
```

```
python make.py dist-clean
```

```
python make.py dist-clean AddonDirectory
```

```
python make.py clean
```

```
python make.py clean AddonDirectory
```
