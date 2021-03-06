#!/usr/bin/env python

import os
import errno
import getpass
import lsst.daf.persistence as dafPersist

def parseInstrument(instrument):
    """Return the mapper class and the intermediate directory (e.g. SUPA or HSC) ."""
    if instrument.lower() in ["hsc", "hscsim"]:
        import lsst.obs.hscSim as obsHsc
        Mapper = obsHsc.HscSimMapper
        addDir = "HSC"
    elif instrument.lower() in ["suprimecam"]:
        import lsst.obs.suprimecam as obsSc
        Mapper = obsSc.SuprimecamMapper
        addDir = "SUPA"
    elif instrument.lower() in ["suprimecam-mit"]:
        import lsst.obs.suprimecam as obsSc
        Mapper = obsSc.SuprimecamMapperMit
        addDir = "SUPA"
    else:
        raise RuntimeError("Unrecognised instrument: %s" % instrument)
    return Mapper, addDir

def getButler(instrument, rerun=None, **kwargs):
    """Return a butler for the appropriate instrument"""
    if rerun is None:
        rerun = getpass.getuser()

    envar = "SUPRIME_DATA_DIR"

    Mapper, addDir = parseInstrument(instrument)

    if kwargs.get('root', None):
        root = kwargs['root']
    else:
        if not os.environ.has_key(envar):
            raise RuntimeError("You must define $%s ; did you setup suprime_data?" % envar)
        
        root = os.path.join(os.environ[envar], addDir)
        kwargs['root'] = root

    if not kwargs.get('outputRoot', None):
        outPath = os.path.join(root, "rerun", rerun)
        kwargs['outputRoot'] = outPath

    mapper = Mapper(**kwargs)

    return dafPersist.ButlerFactory(mapper=mapper).create()


def getNumCcds(instrument):
    """Return the number of CCDs in an instrument"""
    # XXX This could be done by inspecting the number of Ccds in butler.mapper.camera
    if instrument.lower() in ["hsc", "hscsim"]:
        return 104
    if instrument.lower() in ["suprimecam", "suprime-cam", "sc"]:
        return 10
    elif instrument.lower() in ["suprimecam-mit", "mit"]:
        return 10
    raise RuntimeError("Unrecognised instrument: %s" % instrument)

