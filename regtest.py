# RegTest: a really simple IF regression tester.
#   Version 1.4
#   Andrew Plotkin <erkyrath@eblong.com>
#   This script is in the public domain.
#
# For a full description, see <http://eblong.com/zarf/plotex/regtest.html>
#
# (This software is not connected to PlotEx; I'm just distributing them
# from the same folder.)

from __future__ import print_function

import sys
import os
import optparse
import select
import fnmatch
import subprocess
import re
import types

gamefile = None
terppath = None
terpargs = []
remformat = False
precommands = []

checkclasses = []
testmap = {}
testls = []
totalerrors = 0

popt = optparse.OptionParser()

popt.add_option('-g', '--game',
                action='store', dest='gamefile',
                help='game to test')
popt.add_option('-i', '--interpreter', '--terp',
                action='store', dest='terppath',
                help='interpreter to execute')
popt.add_option('-l', '--list',
                action='store_true', dest='listonly',
                help='list all tests (or all matching tests)')
popt.add_option('-p', '--pre', '--precommand',
                action='append', dest='precommands',
                help='extra command to execute before (each) test')
popt.add_option('-c', '--cc', '--checkclass',
                action='append', dest='checkfiles', metavar='FILE',
                help='module containing custom Check classes')
popt.add_option('-r', '--rem',
                action='store_true', dest='remformat',
                help='the interpreter uses RemGlk (JSON) format')
popt.add_option('--vital',
                action='store_true', dest='vital',
                help='abort a test on the first error')
popt.add_option('-v', '--verbose',
                action='store_true', dest='verbose',
                help='display the transcripts as they run')

(opts, args) = popt.parse_args()

if (not args):
    print('usage: regtest.py TESTFILE [ TESTPATS... ]')
    sys.exit(1)

class RegTest:
    """RegTest represents one test in the test file. (That is, a block
    beginning with a single asterisk.)

    A test is one session of the game, from the beginning. (Not necessarily
    to the end.) After every game command, tests can be run.
    """
    def __init__(self, name):
        self.name = name
        self.gamefile = None   # use global gamefile
        self.terp = None       # global terppath, terpargs
        self.precmd = None
        self.cmds = []
    def __repr__(self):
        return '<RegTest %s>' % (self.name,)
    def addcmd(self, cmd):
        self.cmds.append(cmd)

class Command:
    """Command is one cycle of a RegTest -- a game input, followed by
    tests to run on the game's output.
    """
    def __init__(self, cmd, type='line'):
        self.type = type
        if self.type == 'line':
            self.cmd = cmd
        elif self.type == 'char':
            self.cmd = None
            if len(cmd) == 0:
                self.cmd = '\n'
            elif len(cmd) == 1:
                self.cmd = cmd
            elif cmd.lower().startswith('0x'):
                self.cmd = unichr(int(cmd[2:], 16))
            else:
                try:
                    self.cmd = unichr(int(cmd))
                except:
                    pass
            if self.cmd is None:
                raise Exception('Unable to interpret char "%s"' % (cmd,))
        elif self.type == 'include':
            self.cmd = cmd
        elif self.type == 'fileref_prompt':
            self.cmd = cmd
        else:
            raise Exception('Unknown command type: %s' % (type,))
        self.checks = []
    def __repr__(self):
        return '<Command "%s">' % (self.cmd,)
    def addcheck(self, ln):
        args = {}
        # First peel off "!" and "{...}" prefixes
        while True:
            match = re.match('!|{[a-z]*}', ln)
            if not match:
                break
            ln = ln[match.end() : ].strip()
            val = match.group()
            if val == '!' or val == '{invert}':
                args['inverse'] = True
            elif val == '{status}':
                args['instatus'] = True
            elif val == '{vital}':
                args['vital'] = True
            else:
                raise Exception('Unknown test modifier: %s' % (val,))
        # Then the test itself, which may have many formats. We try
        # each of the classes in the checkclasses array until one
        # returns a Check.
        for cla in checkclasses:
            check = cla.buildcheck(ln, args)
            if check is not None:
                self.checks.append(check)
                break
        else:
            raise Exception('Unrecognized test: %s' % (ln,))

class Check:
    """Represents a single test (applied to the output of a game command).

    This can be applied to the story window or the status window. (The
    model is simplistic and assumes there is exactly one story window
    and at most one status window.)

    An "inverse" test has reversed sense.

    A "vital" test will end the test run on failure.
    
    This is a virtual base class. Subclasses should customize the subeval()
    method to examine a list of lines, and return None (on success) or a
    string (explaining the failure).
    """
    inverse = False
    instatus = False

    @classmethod
    def buildcheck(cla, ln, args):
        raise Exception('No buildcheck method defined for class: %s' % (cla.__name__,))
    
    def __init__(self, ln, **args):
        self.inverse = args.get('inverse', False)
        self.instatus = args.get('instatus', False)
        self.vital = args.get('vital', False) or opts.vital
        self.ln = ln
        
    def __repr__(self):
        val = self.ln
        if len(val) > 32:
            val = val[:32] + '...'
        invflag = '!' if self.inverse else ''
        return '<%s %s"%s">' % (self.__class__.__name__, invflag, val,)

    def eval(self, state):
        if self.instatus:
            lines = state.statuswin
        else:
            lines = state.storywin
        res = self.subeval(lines)
        if (not self.inverse):
            return res
        else:
            if res:
                return
            return 'inverse test should fail'
    def subeval(self, lines):
        return 'not implemented'

class RegExpCheck(Check):
    """A Check which looks for a regular expression match in the output.
    """
    @classmethod
    def buildcheck(cla, ln, args):
        # Matches check lines starting with a slash
        if (ln.startswith('/')):
            return RegExpCheck(ln[1:].strip(), **args)
    def subeval(self, lines):
        for ln in lines:
            if re.search(self.ln, ln):
                return
        return 'not found'
        
class LiteralCheck(Check):
    """A Check which looks for a literal string match in the output.
    """
    @classmethod
    def buildcheck(cla, ln, args):
        # Always matches
        return LiteralCheck(ln, **args)
    def subeval(self, lines):
        for ln in lines:
            if self.ln in ln:
                return
        return 'not found'

class GameState:
    """The GameState class wraps the connection to the interpreter subprocess
    (the pipe in and out streams). It's responsible for sending commands
    to the interpreter, and receiving the game output back.

    Currently this class is set up to manage exactly one story window
    and exactly one status window. (A missing window is treated as blank.)
    This is not very general -- we should understand the notion of multiple
    windows -- but it's adequate for now.

    This is a virtual base class. Subclasses should customize the
    initialize, perform_input, and accept_output methods.
    """
    def __init__(self, infile, outfile):
        self.infile = infile
        self.outfile = outfile
        self.statuswin = []
        self.storywin = []

    def initialize(self):
        pass

    def perform_input(self, cmd):
        raise Exception('perform_input not implemented')
        
    def accept_output(self):
        raise Exception('accept_output not implemented')

class GameStateCheap(GameState):
    """Wrapper for a simple stdin/stdout (dumb terminal) interpreter.
    This class never fills in the status window -- that's always blank.
    It can only handle line input (not character input).
    """

    def perform_input(self, cmd):
        if cmd.type != 'line':
            raise Exception('Cheap mode only supports line input')
        self.infile.write(cmd.cmd+'\n')
        self.infile.flush()

    def accept_output(self):
        self.storywin = []
        output = []
        while (select.select([self.outfile],[],[])[0] != []):
            ch = self.outfile.read(1)
            if ch == '':
                break
            output.append(ch)
            if (output[-2:] == ['\n', '>']):
                break
        dat = ''.join(output)
        res = dat.split('\n')
        if (opts.verbose):
            for ln in res:
                if (ln == '>'):
                    continue
                print(ln)
        self.storywin = res
    
class GameStateRemGlk(GameState):
    """Wrapper for a RemGlk-based interpreter. This can in theory handle
    any I/O supported by Glk. But the current implementation is limited
    to line and char input, and no more than one status (grid) window.
    Multiple story (buffer) windows are accepted, but their output for
    a given turn is agglomerated.
    """

    @staticmethod
    def extract_text(line):
        # Extract the text from a line object, ignoring styles.
        con = line.get('content')
        if not con:
            return ''
        dat = [ val.get('text') for val in con ]
        return ''.join(dat)
    
    def initialize(self):
        import json
        update = { 'type':'init', 'gen':0,
                   'metrics': { 'width':80, 'height':40 },
                   }
        cmd = json.dumps(update)
        self.infile.write(cmd+'\n')
        self.infile.flush()
        self.generation = 0
        self.windows = {}
        self.lineinputwin = None
        self.charinputwin = None
        self.specialinput = None
        
    def perform_input(self, cmd):
        import json
        if cmd.type == 'line':
            if not self.lineinputwin:
                raise Exception('Game is not expecting line input')
            update = { 'type':'line', 'gen':self.generation,
                       'window':self.lineinputwin, 'value':cmd.cmd
                       }
        elif cmd.type == 'char':
            if not self.charinputwin:
                raise Exception('Game is not expecting char input')
            val = cmd.cmd
            if val == '\n':
                val = 'return'
            # We should handle arrow keys, too
            update = { 'type':'char', 'gen':self.generation,
                       'window':self.charinputwin, 'value':val
                       }
        elif cmd.type == 'fileref_prompt':
            if self.specialinput != 'fileref_prompt':
                raise Exception('Game is not expecting a fileref_prompt')
            update = { 'type':'specialresponse', 'gen':self.generation,
                       'response':'fileref_prompt', 'value':cmd.cmd
                       }
        else:
            raise Exception('Rem mode does not recognize command type: %s' % (cmd.type))
        cmd = json.dumps(update)
        self.infile.write(cmd+'\n')
        self.infile.flush()
        
    def accept_output(self):
        import json
        output = []
        update = None

        # Read until a complete JSON object comes through the pipe.
        # We sneakily rely on the fact that RemGlk always uses dicts
        # as the JSON object, so it always ends with "}".
        while (select.select([self.outfile],[],[])[0] != []):
            ch = self.outfile.read(1)
            if ch == '':
                # End of stream. Hopefully we have a valid object.
                dat = ''.join(output)
                update = json.loads(dat)
                break
            output.append(ch)
            if (output[-1] == '}'):
                # Test and see if we have a valid object.
                dat = ''.join(output)
                try:
                    update = json.loads(dat)
                    break
                except:
                    pass

        # Parse the update object. This is complicated. For the format,
        # see http://eblong.com/zarf/glk/glkote/docs.html

        self.generation = update.get('gen')

        windows = update.get('windows')
        if windows is not None:
            self.windows = {}
            for win in windows:
                id = win.get('id')
                self.windows[id] = win
            grids = [ win for win in self.windows.values() if win.get('type') == 'grid' ]
            if len(grids) > 1:
                raise Exception('Cannot handle more than one grid window')
            if not grids:
                self.statuswin = []
            else:
                win = grids[0]
                height = win.get('gridheight', 0)
                if height < len(self.statuswin):
                    self.statuswin = self.statuswin[0:height]
                while height > len(self.statuswin):
                    self.statuswin.append('')

        contents = update.get('content')
        if contents is not None:
            for content in contents:
                id = content.get('id')
                win = self.windows.get(id)
                if not win:
                    raise Exception('No such window')
                if win.get('type') == 'buffer':
                    self.storywin = []
                    text = content.get('text')
                    if text:
                        for line in text:
                            dat = self.extract_text(line)
                            if (opts.verbose):
                                if (dat != '>'):
                                    print(dat)
                            if line.get('append') and len(self.storywin):
                                self.storywin[-1] += dat
                            else:
                                self.storywin.append(dat)
                elif win.get('type') == 'grid':
                    lines = content.get('lines')
                    for line in lines:
                        linenum = line.get('line')
                        dat = self.extract_text(line)
                        if linenum >= 0 and linenum < len(self.statuswin):
                            self.statuswin[linenum] = dat

        inputs = update.get('input')
        specialinputs = update.get('specialinput')
        if specialinputs is not None:
            self.specialinput = specialinputs.get('type')
            self.lineinputwin = None
            self.charinputwin = None
        elif inputs is not None:
            self.specialinput = None
            self.lineinputwin = None
            self.charinputwin = None
            for input in inputs:
                if input.get('type') == 'line':
                    if self.lineinputwin:
                        raise Exception('Multiple windows accepting line input')
                    self.lineinputwin = input.get('id')
                if input.get('type') == 'char':
                    if self.charinputwin:
                        raise Exception('Multiple windows accepting char input')
                    self.charinputwin = input.get('id')


checkfile_counter = 0

def parse_checkfile(filename):
    """Load a module containing extra Check subclasses. This is probably
    a terrible abuse of the import mechanism.
    """
    import imp
    global checkfile_counter
    
    modname = '_cc_%d' % (checkfile_counter,)
    checkfile_counter += 1

    fl = open(filename, 'U')
    try:
        mod = imp.load_module(modname, fl, filename, ('.py', 'U', imp.PY_SOURCE))
        for key in dir(mod):
            val = getattr(mod, key)
            if type(val) is types.ClassType and issubclass(val, Check):
                if val is Check:
                    continue
                if val in checkclasses:
                    continue
                checkclasses.insert(0, val)
    finally:
        fl.close()

def parse_tests(filename):
    """Parse the test file. This fills out the testls array, and the
    other globals which will be used during testing.
    """
    global gamefile, terppath, terpargs, remformat
    
    fl = open(filename)
    curtest = None
    curcmd = None

    while True:
        ln = fl.readline()
        if (not ln):
            break
        ln = ln.strip()
        if (not ln or ln.startswith('#')):
            continue

        if (ln.startswith('**')):
            ln = ln[2:].strip()
            pos = ln.find(':')
            if (pos < 0):
                continue
            key = ln[:pos].strip()
            val = ln[pos+1:].strip()
            if not curtest:
                if (key == 'pre' or key == 'precommand'):
                    precommands.append(Command(val))
                elif (key == 'game'):
                    gamefile = val
                elif (key == 'interpreter'):
                    subls = val.split()
                    terppath = subls[0]
                    terpargs = subls[1:]
                elif (key == 'remformat'):
                    remformat = (val.lower() > 'og')
                elif (key == 'checkclass'):
                    parse_checkfile(val)
                else:
                    raise Exception('Unknown option: ** ' + key)
            else:
                if (key == 'game'):
                    curtest.gamefile = val
                elif (key == 'interpreter'):
                    subls = val.split()
                    curtest.terp = (subls[0], subls[1:])
                else:
                    raise Exception('Unknown option: ** ' + key + ' in * ' + curtest.name)
            continue
        
        if (ln.startswith('*')):
            ln = ln[1:].strip()
            if (ln in testmap):
                raise Exception('Test name used twice: ' + ln)
            curtest = RegTest(ln)
            testls.append(curtest)
            testmap[curtest.name] = curtest
            curcmd = Command('(init)')
            curtest.precmd = curcmd
            continue

        if (ln.startswith('>')):
            # Peel off the "{...}" prefix, if found.
            match = re.match('>{([a-z_]*)}', ln)
            if not match:
                cmdtype = 'line'
                ln = ln[1:].strip()
            else:
                cmdtype = match.group(1)
                ln = ln[match.end() : ].strip()
            curcmd = Command(ln, type=cmdtype)
            curtest.addcmd(curcmd)
            continue

        curcmd.addcheck(ln)

    fl.close()


def list_commands(ls, res=None, nested=()):
    """Given a list of commands, replace any {include} commands with the
    commands in the named subtests. This works recursively.
    """
    if res is None:
        res = []
    for cmd in ls:
        if cmd.type == 'include':
            if cmd.cmd in nested:
                raise Exception('Included test includes itself: %s' % (cmd.cmd,))
            test = testmap.get(cmd.cmd)
            if not test:
                raise Exception('Included test not found: %s' % (cmd.cmd,))
            list_commands(test.cmds, res, nested+(cmd.cmd,))
            continue
        res.append(cmd)
    return res

class VitalCheckException(Exception):
    pass

def run(test):
    """Run a single RegTest.
    """
    global totalerrors

    testgamefile = gamefile
    if (test.gamefile):
        testgamefile = test.gamefile
    testterppath, testterpargs = (terppath, terpargs)
    if (test.terp):
        testterppath, testterpargs = test.terp
    
    print('* ' + test.name)
    args = [ testterppath ] + testterpargs + [ testgamefile ]
    proc = subprocess.Popen(args,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    if (not remformat):
        gamestate = GameStateCheap(proc.stdin, proc.stdout)
    else:
        gamestate = GameStateRemGlk(proc.stdin, proc.stdout)


    cmdlist = list_commands(precommands + test.cmds)

    try:
        gamestate.initialize()
        gamestate.accept_output()
        if (test.precmd):
            for check in test.precmd.checks:
                res = check.eval(gamestate)
                if (res):
                    totalerrors += 1
                    val = '*** ' if opts.verbose else ''
                    print('%s%s: %s' % (val, check, res))
                    if check.vital:
                        raise VitalCheckException()
    
        for cmd in cmdlist:
            if (opts.verbose):
                if cmd.type == 'line':
                    if (not remformat):
                        print('> %s' % (cmd.cmd,))
                    else:
                        # The input line is echoed by the game.
                        print('>', end='')
                else:
                    print('> {%s} %s' % (cmd.type, repr(cmd.cmd),))
            gamestate.perform_input(cmd)
            gamestate.accept_output()
            for check in cmd.checks:
                res = check.eval(gamestate)
                if (res):
                    totalerrors += 1
                    val = '*** ' if opts.verbose else ''
                    print('%s%s: %s' % (val, check, res))
                    if check.vital:
                        raise VitalCheckException()

    except VitalCheckException, ex:
        # An error has already been logged; just fall out.
        pass
    except Exception, ex:
        totalerrors += 1
        val = '*** ' if opts.verbose else ''
        print('%s%s: %s' % (val, ex.__class__.__name__, ex))

    gamestate = None
    proc.stdin.close()
    proc.stdout.close()
    proc.kill()
    proc.poll()
    
    
checkclasses.append(RegExpCheck)
checkclasses.append(LiteralCheck)
if (opts.checkfiles):
    for cc in opts.checkfiles:
        parse_checkfile(cc)

parse_tests(args[0])

if (len(args) <= 1):
    testnames = ['*']
else:
    testnames = args[1:]

if (opts.gamefile):
    gamefile = opts.gamefile
if (not gamefile):
    print('No game file specified')
    sys.exit(-1)

if (opts.terppath):
    terppath = opts.terppath
if (not terppath):
    print('No interpreter path specified')
    sys.exit(-1)
if (opts.remformat):
    remformat = True

if (opts.precommands):
    for cmd in opts.precommands:
        precommands.append(Command(cmd))

testcount = 0
for test in testls:
    use = False
    for pat in testnames:
        if pat == '*' and (test.name.startswith('-') or test.name.startswith('_')):
            continue
        if (fnmatch.fnmatch(test.name, pat)):
            use = True
            break
    if (use):
        testcount += 1
        if (opts.listonly):
            print(test.name)
        else:
            run(test)

if (not testcount):
    print('No tests performed!')
if (totalerrors):
    print()
    print('FAILED: %d errors' % (totalerrors,))
