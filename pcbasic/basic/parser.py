"""
PC-BASIC - parser.py
BASIC code parser

(c) 2013, 2014, 2015, 2016 Rob Hagemans
This file is released under the GNU GPL version 3 or later.
"""

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO
import string
from collections import deque

from . import error
from . import fp
from . import representation
from . import basictoken as tk
from . import util
from . import vartypes
from . import statements
from . import operators as op
from . import functions


class Parser(object):
    """Statement parser."""

    def __init__(self, session, syntax, term, double_math):
        """Initialise parser."""
        self.session = session
        # syntax: advanced, pcjr, tandy
        self.syntax = syntax
        # program for TERM command
        self.term = term
        # line number tracing
        self.tron = False
        # pointer position: False for direct line, True for program
        self.run_mode = False
        self.program_code = session.program.bytecode
        self.current_statement = 0
        # clear stacks
        self.clear_stacks_and_pointers()
        self.init_error_trapping()
        self.error_num = 0
        self.error_pos = 0
        self.double_math = double_math
        self.statements = statements.Statements(self)
        self.operators = op.Operators(session.strings, double_math)
        self.functions = functions.Functions(self, double_math)


    def init_error_trapping(self):
        """Initialise error trapping."""
        # True if error handling in progress
        self.error_handle_mode = False
        # statement pointer, run mode of error for RESUME
        self.error_resume = None
        # pointer to error trap
        self.on_error = None

    def parse_statement(self):
        """Parse one statement at the current pointer in current codestream.
            Return False if stream has ended, True otherwise.
            """
        try:
            self.handle_basic_events()
            ins = self.get_codestream()
            self.current_statement = ins.tell()
            c = util.skip_white(ins)
            if c == '':
                # stream has ended.
                self.set_pointer(False)
                return False
            # parse line number or : at start of statement
            elif c == '\0':
                # save position for error message
                prepos = ins.tell()
                ins.read(1)
                # line number marker, new statement
                linenum = util.parse_line_number(ins)
                if linenum == -1:
                    if self.error_resume:
                        # unfinished error handler: no RESUME (don't trap this)
                        self.error_handle_mode = True
                        # get line number right
                        raise error.RunError(error.NO_RESUME, prepos-1)
                    # stream has ended
                    self.set_pointer(False)
                    return False
                if self.tron:
                    self.session.screen.write('[' + ('%i' % linenum) + ']')
                self.session.debugger.debug_step(linenum)
            elif c == ':':
                ins.read(1)
            c = util.skip_white(ins)
            # empty statement, return to parse next
            if c in tk.end_statement:
                return True
            # implicit LET
            elif c in string.ascii_letters:
                self.statements.exec_let(ins)
            # token
            else:
                ins.read(1)
                if c in tk.twobyte:
                    c += ins.read(1)
                # don't use try-block to avoid catching other KeyErrors in statement
                if c not in self.statements.statements:
                    raise error.RunError(error.STX)
                self.statements.statements[c](ins)
        except error.RunError as e:
            self.trap_error(e)
        return True

    #################################################################

    def clear(self):
        """Clear all to be cleared for CLEAR statement."""
        # clear last error number (ERR) and line number (ERL)
        self.error_num, self.error_pos = 0, 0
        # disable error trapping
        self.init_error_trapping()
        # disable all event trapping (resets PEN to OFF too)
        self.session.events.reset()
        # CLEAR also dumps for_next and while_wend stacks
        self.clear_loop_stacks()
        # reset the DATA pointer
        self.restore()

    def clear_stacks_and_pointers(self):
        """Initialise the stacks and pointers for a new program."""
        # stop running if we were
        self.set_pointer(False)
        # reset loop stacks
        self.clear_stacks()
        # reset program pointer
        self.program_code.seek(0)
        # reset stop/cont
        self.stop = None
        # reset data reader
        self.restore()

    def clear_stacks(self):
        """Clear loop and jump stacks."""
        self.gosub_stack = []
        self.clear_loop_stacks()

    def clear_loop_stacks(self):
        """Clear loop stacks."""
        self.for_stack = []
        self.while_stack = []

    #################################################################

    def handle_basic_events(self):
        """Jump to user-defined event subs if events triggered."""
        if self.session.events.suspend_all or not self.run_mode:
            return
        for event in self.session.events.all:
            if (event.enabled and event.triggered
                    and not event.stopped and event.gosub is not None):
                # release trigger
                event.triggered = False
                # stop this event while handling it
                event.stopped = True
                # execute 'ON ... GOSUB' subroutine;
                # attach handler to allow un-stopping event on RETURN
                self.jump_gosub(event.gosub, event)

    def trap_error(self, e):
        """Handle a BASIC error through trapping."""
        if e.pos is None:
            if self.run_mode:
                e.pos = self.program_code.tell()-1
            else:
                e.pos = -1
        self.error_num = e.err
        self.error_pos = e.pos
        # don't jump if we're already busy handling an error
        if self.on_error is not None and self.on_error != 0 and not self.error_handle_mode:
            self.error_resume = self.current_statement, self.run_mode
            self.jump(self.on_error)
            self.error_handle_mode = True
            self.session.events.suspend_all = True
        else:
            self.error_handle_mode = False
            self.set_pointer(False)
            raise e

    #################################################################

    def set_pointer(self, new_runmode, pos=None):
        """Set program pointer to the given codestream and position."""
        self.run_mode = new_runmode
        # events are active in run mode
        self.session.events.set_active(new_runmode)
        # keep the sound engine on to avoid delays in run mode
        self.session.sound.persist(new_runmode)
        # suppress cassette messages in run mode
        self.session.devices.devices['CAS1:'].quiet(new_runmode)
        codestream = self.get_codestream()
        if pos is not None:
            # jump to position, if given
            codestream.seek(pos)
        else:
            # position at end - don't execute anything unless we jump
            codestream.seek(0, 2)

    def get_codestream(self):
        """Get the current codestream."""
        return self.program_code if self.run_mode else self.session.direct_line

    def jump(self, jumpnum, err=error.UNDEFINED_LINE_NUMBER):
        """Execute jump for a GOTO or RUN instruction."""
        if jumpnum is None:
            self.set_pointer(True, 0)
        else:
            try:
                # jump to target
                self.set_pointer(True, self.session.program.line_numbers[jumpnum])
            except KeyError:
                raise error.RunError(err)

    def jump_gosub(self, jumpnum, handler=None):
        """Execute jump for a GOSUB."""
        # set return position
        self.gosub_stack.append((self.get_codestream().tell(), self.run_mode, handler))
        self.jump(jumpnum)

    def jump_return(self, jumpnum):
        """Execute jump for a RETURN."""
        try:
            pos, orig_runmode, handler = self.gosub_stack.pop()
        except IndexError:
            raise error.RunError(error.RETURN_WITHOUT_GOSUB)
        # returning from ON (event) GOSUB, re-enable event
        if handler:
            # if stopped explicitly using STOP, we wouldn't have got here; it STOP is run  inside the trap, no effect. OFF in trap: event off.
            handler.stopped = False
        if jumpnum is None:
            # go back to position of GOSUB
            self.set_pointer(orig_runmode, pos)
        else:
            # jump to specified line number
            self.jump(jumpnum)


    #################################################################

    def loop_init(self, ins, forpos, nextpos, varname, start, stop, step):
        """Initialise a FOR loop."""
        # set start to start-step, then iterate - slower on init but allows for faster iterate
        self.session.scalars.set(varname, op.Operators.number_add(start, op.Operators.number_neg(step)))
        # NOTE: all access to varname must be in-place into the bytearray - no assignments!
        sgn = vartypes.integer_to_int_signed(op.Operators.number_sgn(step))
        self.for_stack.append(
            (forpos, nextpos, varname[-1],
                self.session.scalars.variables[varname],
                vartypes.number_unpack(stop), vartypes.number_unpack(step), sgn))
        ins.seek(nextpos)

    def number_inc_gt(self, typechar, loopvar, stop, step, sgn):
        """Increase number and check if it exceeds a limit."""
        if sgn == 0:
            return False
        if typechar in ('#', '!'):
            fp_left = fp.from_bytes(loopvar).iadd(step)
            loopvar[:] = fp_left.to_bytes()
            return fp_left.gt(stop) if sgn > 0 else stop.gt(fp_left)
        else:
            int_left = vartypes.integer_to_int_signed(vartypes.bytes_to_integer(loopvar)) + step
            loopvar[:] = vartypes.integer_to_bytes(vartypes.int_to_integer_signed(int_left))
            return int_left > stop if sgn > 0 else stop > int_left

    def loop_iterate(self, ins, pos):
        """Iterate a loop (NEXT)."""
        # find the matching NEXT record
        num = len(self.for_stack)
        for depth in range(num):
            forpos, nextpos, typechar, loopvar, stop, step, sgn = self.for_stack[-depth-1]
            if pos == nextpos:
                # only drop NEXT record if we've found a matching one
                self.for_stack = self.for_stack[:len(self.for_stack)-depth]
                break
        else:
            raise error.RunError(error.NEXT_WITHOUT_FOR)
        # increment counter
        loop_ends = self.number_inc_gt(typechar, loopvar, stop, step, sgn)
        if loop_ends:
            self.for_stack.pop()
        else:
            ins.seek(forpos)
        return not loop_ends

    #################################################################
    # DATA utilities

    def restore(self, datanum=-1):
        """Reset data pointer (RESTORE) """
        try:
            self.data_pos = 0 if datanum == -1 else self.session.program.line_numbers[datanum]
        except KeyError:
            raise error.RunError(error.UNDEFINED_LINE_NUMBER)

    def read_entry(self):
        """READ a unit of DATA."""
        current = self.program_code.tell()
        self.program_code.seek(self.data_pos)
        if util.peek(self.program_code) in tk.end_statement:
            # initialise - find first DATA
            util.skip_to(self.program_code, ('\x84',))  # DATA
        if self.program_code.read(1) not in ('\x84', ','):
            raise error.RunError(error.OUT_OF_DATA)
        vals, word, literal = '', '', False
        while True:
            # read next char; omit leading whitespace
            if not literal and vals == '':
                c = util.skip_white(self.program_code)
            else:
                c = util.peek(self.program_code)
            # parse char
            if c == '' or (not literal and c == ',') or (c in tk.end_line or (not literal and c in tk.end_statement)):
                break
            elif c == '"':
                self.program_code.read(1)
                literal = not literal
                if not literal:
                    util.require(self.program_code, tk.end_statement + (',',))
            else:
                self.program_code.read(1)
                if literal:
                    vals += c
                else:
                    word += c
                # omit trailing whitespace
                if c not in tk.whitespace:
                    vals += word
                    word = ''
        self.data_pos = self.program_code.tell()
        self.program_code.seek(current)
        return vals

    ###########################################################################
    # expression parser

    def parse_bracket(self, ins, session):
        """Compute the value of the bracketed expression."""
        util.require_read(ins, ('(',))
        # we'll get a Syntax error, not a Missing operand, if we close with )
        val = self.parse_expression(ins, session)
        util.require_read(ins, (')',))
        return val

    def parse_literal(self, ins, session):
        """Compute the value of the literal at the current code pointer."""
        d = util.skip_white(ins)
        # string literal
        if d == '"':
            ins.read(1)
            if ins == session.program.bytecode:
                address = ins.tell() + session.memory.code_start
            else:
                address = None
            output = bytearray()
            # while tokenised numbers inside a string literal will be printed as tokenised numbers, they don't actually execute as such:
            # a \00 character, even if inside a tokenised number, will break a string literal (and make the parser expect a
            # line number afterwards, etc. We follow this.
            d = ins.read(1)
            while d not in tk.end_line + ('"',):
                output += d
                d = ins.read(1)
            if d == '\0':
                ins.seek(-1, 1)
            # store for easy retrieval, but don't reserve space in string memory
            return session.strings.store(output, address)
        # number literals as ASCII are accepted in tokenised streams. only if they start with a figure (not & or .)
        # this happens e.g. after non-keywords like AS. They are not acceptable as line numbers.
        elif d in string.digits:
            outs = StringIO()
            representation.tokenise_number(ins, outs)
            outs.seek(0)
            return representation.parse_value(outs)
        # number literals
        elif d in tk.number:
            return representation.parse_value(ins)
        # gw-basic allows adding line numbers to numbers
        elif d == tk.T_UINT:
            return vartypes.int_to_integer_unsigned(util.parse_jumpnum(ins))
        else:
            raise error.RunError(error.STX)

    def parse_variable(self, ins, session):
        """Helper function: parse a variable or array element."""
        name = self.parse_scalar(ins)
        indices = []
        if util.skip_white_read_if(ins, ('[', '(')):
            # it's an array, read indices
            while True:
                indices.append(vartypes.pass_int_unpack(self.parse_expression(ins, session)))
                if not util.skip_white_read_if(ins, (',',)):
                    break
            util.require_read(ins, (']', ')'))
        return name, indices

    def parse_scalar(self, ins, allow_empty=False, err=error.STX):
        """Get variable name from token stream."""
        # append type specifier
        name = self.session.memory.complete_name(util.read_name(ins, allow_empty, err))
        # only the first 40 chars are relevant in GW-BASIC, rest is discarded
        if len(name) > 41:
            name = name[:40]+name[-1]
        return name.upper()

    def parse_file_number(self, ins, session, file_mode='IOAR'):
        """Helper function: parse a file number and retrieve the file object."""
        screen = None
        if util.skip_white_read_if(ins, ('#',)):
            number = vartypes.pass_int_unpack(self.parse_expression(ins, session))
            util.range_check(0, 255, number)
            screen = self.session.files.get(number, file_mode)
            util.require_read(ins, (',',))
        return screen

    def parse_file_number_opthash(self, ins, session):
        """Helper function: parse a file number, with optional hash."""
        util.skip_white_read_if(ins, ('#',))
        number = vartypes.pass_int_unpack(self.parse_expression(ins, session))
        util.range_check(0, 255, number)
        return number

    def parse_expression(self, ins, session, allow_empty=False):
        """Compute the value of the expression at the current code pointer."""
        stack = deque()
        units = deque()
        d = ''
        missing_error = error.MISSING_OPERAND
        # see https://en.wikipedia.org/wiki/Shunting-yard_algorithm
        while True:
            last = d
            d = util.skip_white(ins)
            # two-byte function tokens
            if d in tk.twobyte:
                d = util.peek(ins, n=2)
            if d == tk.NOT and not (last in op.operators or last == ''):
                # unary NOT ends expression except after another operator or at start
                break
            elif d in op.operators:
                ins.read(len(d))
                # get combined operators such as >=
                if d in op.combinable:
                    nxt = util.skip_white(ins)
                    if nxt in op.combinable:
                        d += ins.read(len(nxt))
                if last in op.operators or last == '' or d == tk.NOT:
                    # also if last is ( but that leads to recursive call and last == ''
                    nargs = 1
                    # zero operands for a binary operator is always syntax error
                    # because it will be seen as an illegal unary
                    if d not in self.operators.unary:
                        raise error.RunError(error.STX)
                else:
                    nargs = 2
                    if d not in op.operators:
                        # illegal combined ops like == raise syntax error
                        raise error.RunError(error.STX)
                    self._evaluate_stack(stack, units, op.precedence[d], error.STX)
                stack.append((d, nargs))
            elif not (last in op.operators or last == ''):
                # repeated unit ends expression
                # repeated literals or variables or non-keywords like 'AS'
                break
            elif d == '(':
                units.append(self.parse_bracket(ins, session))
            elif d and d in string.ascii_letters:
                # variable name
                name, indices = self.parse_variable(ins, session)
                units.append(self.session.memory.get_variable(name, indices))
            elif d in self.functions.functions:
                # apply functions
                ins.read(len(d))
                try:
                    units.append(self.functions.functions[d](ins))
                except (ValueError, ArithmeticError) as e:
                    units.append(self._handle_math_error(e))
            elif d in tk.end_statement:
                break
            elif d in tk.end_expression:
                # missing operand inside brackets or before comma is syntax error
                missing_error = error.STX
                break
            else:
                # literal
                units.append(self.parse_literal(ins, session))
        # empty expression is a syntax error (inside brackets)
        # or Missing Operand (in an assignment)
        # or not an error (in print and many functions)
        if units or stack:
            self._evaluate_stack(stack, units, 0, missing_error)
            return units[0]
        elif allow_empty:
            return None
        else:
            raise error.RunError(missing_error)

    def _evaluate_stack(self, stack, units, precedence, missing_err):
        """Drain evaluation stack until an operator of low precedence on top."""
        while stack:
            if precedence > op.precedence[stack[-1][0]]:
                break
            oper, narity = stack.pop()
            try:
                right = units.pop()
                if narity == 1:
                    units.append(self.operators.unary[oper](right))
                else:
                    left = units.pop()
                    units.append(self.operators.binary[oper](left, right))
            except IndexError:
                # insufficient operators, error depends on context
                raise error.RunError(missing_err)
            except (ValueError, ArithmeticError) as e:
                units.append(self._handle_math_error(e))

    def _handle_math_error(self, e):
        """Handle Overflow or Division by Zero."""
        if isinstance(e, ValueError):
            # math domain errors such as SQR(-1)
            raise error.RunError(error.IFC)
        elif isinstance(e, OverflowError):
            math_error = error.OVERFLOW
        elif isinstance(e, ZeroDivisionError):
            math_error = error.DIVISION_BY_ZERO
        else:
            raise e
        if self.session.parser.on_error:
            # also raises exception in error_handle_mode!
            # in that case, prints a normal error message
            raise error.RunError(math_error)
        else:
            # write a message & continue as normal
            self.session.screen.write_line(error.RunError(math_error).message)
        # return max value for the appropriate float type
        if e.args and e.args[0] and isinstance(e.args[0], fp.Float):
            return fp.pack(e.args[0])
        return fp.pack(fp.Single.max.copy())
