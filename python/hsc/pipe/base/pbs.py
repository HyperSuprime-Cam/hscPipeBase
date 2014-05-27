#!/usr/bin/env python

import hsc.pipe.base.log # Monkey-patch lsst.pex.logging

import re
import os
import os.path
import stat
import sys
import tempfile
import argparse
import lsst.pex.logging as pexLog
import lsst.afw.cameraGeom as cameraGeom
from lsst.pipe.base import CmdLineTask
from hsc.pipe.base.pool import startPool

UMASK = "002" # umask to set

# Functions to convert a list of arguments to a quoted shell command, provided by Dave Abrahams
# http://stackoverflow.com/questions/967443/python-module-to-shellquote-unshellquote
_quote_pos = re.compile('(?=[^-0-9a-zA-Z_./\n])')
def shQuote(arg):
    r"""Quote the argument for the shell.

    >>> quote('\t')
    '\\\t'
    >>> quote('foo bar')
    'foo\\ bar'
    """
    # This is the logic emacs uses
    if arg:
        return _quote_pos.sub('\\\\', arg).replace('\n',"'\n'")
    else:
        return "''"
def shCommandFromArgs(args):
    """Convert a list of shell arguments to a shell command-line"""
    return ' '.join([shQuote(a) for a in args])


class PbsArgumentParser(argparse.ArgumentParser):
    """An argument parser to get relevant parameters for PBS.

    We want to be able to display the help for a 'parent' ArgumentParser
    along with the PBS-specific options we introduce in this class, but
    we don't want to swallow the parent (i.e., ArgumentParser(parents=[parent]))
    because we want to save the list of arguments that this particular
    PbsArgumentParser doesn't parse, so they can be passed on to a different
    program (though we also want to parse them to check that they can be parsed).
    """
    def __init__(self, parent=None, *args, **kwargs):
        super(PbsArgumentParser, self).__init__(*args, **kwargs)
        self._parent = parent
        group = self.add_argument_group("PBS options")
        group.add_argument("--queue", help="PBS queue name")
        group.add_argument("--job", help="Job name")
        group.add_argument("--nodes", type=int, default=1, help="Number of nodes")
        group.add_argument("--procs", type=int, default=1, help="Number of processors per node")
        group.add_argument("--pbs-group", dest="pbsGroup", default=None, help="Group (property) of nodes to assign jobs")
        group.add_argument("--time", type=float, default=1000,
                           help="Expected execution time per element (sec)")
        group.add_argument("--pbs-output", dest="pbsOutput", help="Output directory")
        group.add_argument("--dry-run", dest="dryrun", default=False, action="store_true",
                           help="Dry run?")
        group.add_argument("--do-exec", dest="doExec", default=False, action="store_true",
                           help="Exec script instead of qsub?")
        group.add_argument("--mpiexec", default="", help="mpiexec options")

    def parse_args(self, config=None, args=None, namespace=None, **kwargs):
        args, leftover = super(PbsArgumentParser, self).parse_known_args(args=args, namespace=namespace)
        args.parent = None
        args.leftover = None
        if len(leftover) > 0:
            # Save any leftovers for the parent
            if self._parent is None:
                self.error("Unrecognised arguments: %s" % leftover)
            args.parent = self._parent.parse_args(config, args=leftover, **kwargs)
            args.leftover = leftover
        args.pbs = Pbs(outputDir=args.pbsOutput, numNodes=args.nodes, numProcsPerNode=args.procs,
                       groupNodes=args.pbsGroup,                       
                       walltime=args.time, queue=args.queue, jobName=args.job, dryrun=args.dryrun,
                       doExec=args.doExec, mpiexec=args.mpiexec)
        return args

    def format_help(self):
        text = """This is a script for PBS submission of a wrapped script.

Use this program name and ignore that for the wrapped script (it will be
passed on to PBS).  Arguments for *both* the PBS wrapper script or the
wrapped script are valid (if it is required for the wrapped script, it
is required for the wrapper as well).

*** PBS submission wrapper:

"""
        text += super(PbsArgumentParser, self).format_help()
        if self._parent is not None:
            text += """

*** Wrapped script:

"""
            text += self._parent.format_help()
        return text

    def format_usage(self):
        if self._parent is not None:
            prog = self._parent.prog
            self._parent.prog = self.prog
            usage = self._parent.format_usage()
            self._parent.prog = prog
            return usage
        return super(PbsArgumentParser, self).format_usage()


class Pbs(object):
    def __init__(self, outputDir=None, numNodes=1, numProcsPerNode=1, queue=None, jobName=None, walltime=None,
                 groupNodes=None, 
                 dryrun=False, doExec=False, mpiexec=""):
        self.outputDir = outputDir
        self.numNodes = numNodes
        self.numProcsPerNode = numProcsPerNode
        self.queue = queue
        self.jobName = jobName
        self.walltime = walltime
        self.groupNodes = groupNodes
        self.dryrun = dryrun
        self.doExec = doExec
        self.mpiexec = mpiexec

    def create(self, command, walltime=None, numNodes=None, numProcsPerNode=None, jobName=None,
               groupNodes=None,
               threads=None):
        if walltime is None:
            walltime = self.walltime
        if numNodes is None:
            numNodes = self.numNodes
        if numProcsPerNode is None:
            numProcsPerNode = self.numProcsPerNode
        if groupNodes is None:
            groupNodes = self.groupNodes
        if jobName is None:
            jobName = self.jobName
        if threads is None:
            threads = numNodes * numProcsPerNode
        threads = min(threads, numNodes * numProcsPerNode)

        fd, script = tempfile.mkstemp()
        f = os.fdopen(fd, "w")

        if numNodes is None or numProcsPerNode is None:
            raise RuntimeError("numNodes (%s) or numProcsPerNode (%s) is not specified" %
                               (numNodes, numProcsPerNode))

        assert numNodes is not None and numProcsPerNode is not None
        if jobName is None:
            # Name of executable without path
            jobName = command[:command.find(" ")]
            jobName = jobName[jobName.rfind("/"):]

        print >>f, "#!/bin/bash"
        print >>f, "#   Post this job with `qsub -V $0'"
        if groupNodes is not None:
            print >>f, "#PBS -l nodes=%d:ppn=%d:%s" % (numNodes, numProcsPerNode, groupNodes)
        else:
            print >>f, "#PBS -l nodes=%d:ppn=%d" % (numNodes, numProcsPerNode)
        if walltime is not None:
            print >>f, "#PBS -l walltime=%d" % walltime
        if self.outputDir is not None:
            print >>f, "#PBS -o %s" % self.outputDir
        print >>f, "#PBS -N %s" % jobName
        if self.queue is not None:
            print >>f, "#PBS -q %s" % self.queue
        print >>f, "#PBS -j oe"
        print >>f, "#PBS -W umask=%s" % UMASK
        print >>f, exportEnv()
        print >>f, "echo \"mpiexec is at: $(which mpiexec)\""
        print >>f, "ulimit -a"
        print >>f, "umask %s" % UMASK
        print >>f, "echo 'umask: ' $(umask)"
        print >>f, "eups list -s"
        print >>f, "export"
        print >>f, "cd %s" % os.getcwd()
        print >>f, "mpiexec %s %s" % (self.mpiexec, command)
        f.close()
        os.chmod(script, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return script

    def run(self, command, *args, **kwargs):
        script = self.create(command, *args, **kwargs)
        command = "qsub -V %s" % script
        if self.dryrun:
            print "Would run: %s" % command
        elif self.doExec:
            os.execl(script, script)
        else:
            os.system(command)
        return script


def exportEnv():
    """Generate bash script to regenerate the current environment"""
    output = ""
    for key, val in os.environ.items():
        if key in ("DISPLAY",):
            continue
        if val.startswith("() {"):
            # This is a function.
            # "Two parentheses, a single space, and a brace"
            # is exactly the same criterion as bash uses.
            output += "function {key} {val}\nexport -f {key}\n".format(key=key, val=val)
        else:
            # This is a variable.
            output += "export {key}='{val}'\n".format(key=key, val=val.replace("'", "'\"'\"'"))
    return output


def submitPbs(TaskClass, description, command):
    processParser = TaskClass._makeArgumentParser(add_help=False)
    pbsParser = PbsArgumentParser(description=description, parent=processParser)
    args = pbsParser.parse_args(TaskClass.ConfigClass())

    numExps = len(args.parent.id.refList if args.parent is not None else [])
    if numExps == 0:
        print "No frames provided to process"
        exit(1)

    numCcds = sum([sum([1 for ccd in cameraGeom.cast_Raft(raft)])
                   for raft in args.parent.butler.mapper.camera])
    command = "python %s %s" % (command, shCommandFromArgs(args.leftover))
    args.pbs.run(command, repeats=numExps, threads=numCcds)


class PbsCmdLineTask(CmdLineTask):
    @classmethod
    def parseAndSubmit(cls, args=None, **kwargs):
        taskParser = cls._makeArgumentParser(doPbs=True, add_help=False)
        pbsParser = PbsArgumentParser(parent=taskParser)
        pbsArgs = pbsParser.parse_args(config=cls.ConfigClass(), args=args, **kwargs)

        walltime = cls.pbsWallTime(pbsArgs.time, pbsArgs.parent, pbsArgs.nodes, pbsArgs.procs)

        command = cls.pbsCommand(pbsArgs)
        pbsArgs.pbs.run(command, walltime=walltime)

    @classmethod
    def pbsWallTime(cls, time, parsedCmd, numNodes, numProcs):
        """Return walltime request for PBS

        Subclasses should override if the walltime should be calculated
        differently (e.g., addition of some serial time).

        @param time: Requested time per iteration
        @param parsedCmd: Results of argument parsing
        @param numNodes: Number of nodes for processing
        @param numProcs: Number of processors per node
        """
        numTargets = len(cls.RunnerClass.getTargetList(parsedCmd))
        return time*numTargets/(numNodes*numProcs)

    @classmethod
    def pbsCommand(cls, args):
        """Return command to run CmdLineTask

        @param args: Parsed PBS arguments (from PbsArgumentParser)
        """
        module = cls.__module__
        return ("python -c 'import os; os.umask(%s); " +
                "import hsc.pipe.base.log; hsc.pipe.base.log.jobLog(\"%s\"); " +
                "import %s; %s.%s.parseAndRun()' %s") % (UMASK, args.job, module, module, cls.__name__,
                                                         shCommandFromArgs(args.leftover))


class PbsPoolTask(PbsCmdLineTask):
    @classmethod
    def parseAndRun(cls, *args, **kwargs):
        """Run with a MPI process pool"""
        pool = startPool()
        super(PbsPoolTask, cls).parseAndRun(*args, **kwargs)
        pool.exit()
