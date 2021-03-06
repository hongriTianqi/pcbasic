"""
PC-BASIC - program.py
Program buffer utilities

(c) 2013, 2014, 2015, 2016 Rob Hagemans
This file is released under the GNU GPL version 3 or later.
"""

import logging

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

from . import error
from . import vartypes
from . import basictoken as tk
from . import protect
from . import util

class Program(object):
    """BASIC program."""

    def __init__(self, tokeniser, max_list_line=65536,
                allow_protect=False, allow_code_poke=False, address=0):
        """Initialise program."""
        # program bytecode buffer
        self.bytecode = StringIO()
        self.erase()
        self.max_list_line = max_list_line
        self.allow_protect = allow_protect
        self.allow_code_poke = allow_code_poke
        # to be set when file memory is initialised
        self.code_start = address
        # for detokenise_line()
        self.tokeniser = tokeniser

    def set_address(self, code_start):
        """Memory location of program."""
        self.code_start = code_start

    def size(self):
        """Size of code space """
        return len(self.bytecode.getvalue())

    def erase(self):
        """Erase the program from memory."""
        self.bytecode.truncate(0)
        self.bytecode.write('\0\0\0')
        self.protected = False
        self.line_numbers = { 65536: 0 }
        self.last_stored = None

    def truncate(self, rest=''):
        """Write bytecode and cut the program of beyond the current position."""
        self.bytecode.write(rest if rest else '\0\0\0')
        # cut off at current position
        self.bytecode.truncate()

    def get_line_number(self, pos):
        """Get line number for stream position."""
        pre = -1
        for linum in self.line_numbers:
            linum_pos = self.line_numbers[linum]
            if linum_pos <= pos and linum > pre:
                pre = linum
        return pre

    def rebuild_line_dict(self):
        """Preparse to build line number dictionary."""
        self.line_numbers, offsets = {}, []
        self.bytecode.seek(0)
        scanline, scanpos, last = 0, 0, 0
        while True:
            self.bytecode.read(1) # pass \x00
            scanline = util.parse_line_number(self.bytecode)
            if scanline == -1:
                scanline = 65536
                # if parse_line_number returns -1, it leaves the stream pointer here: 00 _00_ 00 1A
                break
            self.line_numbers[scanline] = scanpos
            last = scanpos
            util.skip_to(self.bytecode, tk.end_line)
            scanpos = self.bytecode.tell()
            offsets.append(scanpos)
        self.line_numbers[65536] = scanpos
        # rebuild offsets
        self.bytecode.seek(0)
        last = 0
        for pos in offsets:
            self.bytecode.read(1)
            self.bytecode.write(str(vartypes.integer_to_bytes(vartypes.int_to_integer_unsigned((self.code_start + 1) + pos))))
            self.bytecode.read(pos - last - 3)
            last = pos
        # ensure program is properly sealed - last offset must be 00 00. keep, but ignore, anything after.
        self.bytecode.write('\0\0\0')

    def update_line_dict(self, pos, afterpos, length, deleteable, beyond):
        """Update line number dictionary after deleting lines."""
        # subtract length of line we replaced
        length -= afterpos - pos
        addr = (self.code_start + 1) + afterpos
        self.bytecode.seek(afterpos + length + 1)  # pass \x00
        while True:
            next_addr = self.bytecode.read(2)
            if len(next_addr) < 2 or next_addr == '\0\0':
                break
            next_addr = vartypes.integer_to_int_unsigned(vartypes.bytes_to_integer(next_addr))
            self.bytecode.seek(-2, 1)
            self.bytecode.write(str(vartypes.integer_to_bytes(vartypes.int_to_integer_unsigned(next_addr + length))))
            self.bytecode.read(next_addr - addr - 2)
            addr = next_addr
        # update line number dict
        for key in deleteable:
            del self.line_numbers[key]
        for key in beyond:
            self.line_numbers[key] += length

    def check_number_start(self, linebuf):
        """Check if the given line buffer starts with a line number."""
        # get the new line number
        linebuf.seek(1)
        scanline = util.parse_line_number(linebuf)
        c = util.skip_white_read(linebuf)
        # check if linebuf is an empty line after the line number
        empty = (c in tk.end_line)
        # check if we start with a number
        if c in tk.number:
            raise error.RunError(error.STX)
        return empty, scanline

    def store_line(self, linebuf):
        """Store the given line buffer."""
        if self.protected:
            raise error.RunError(error.IFC)
        # get the new line number
        linebuf.seek(1)
        scanline = util.parse_line_number(linebuf)
        # check if linebuf is an empty line after the line number
        empty = (util.skip_white_read(linebuf) in tk.end_line)
        pos, afterpos, deleteable, beyond = self.find_pos_line_dict(scanline, scanline)
        if empty and not deleteable:
            raise error.RunError(error.UNDEFINED_LINE_NUMBER)
        # read the remainder of the program into a buffer to be pasted back after the write
        self.bytecode.seek(afterpos)
        rest = self.bytecode.read()
        # insert
        self.bytecode.seek(pos)
        # write the line buffer to the program buffer
        length = 0
        if not empty:
            # set offsets
            linebuf.seek(3) # pass \x00\xC0\xDE
            length = len(linebuf.getvalue())
            self.bytecode.write('\0' +
                str(vartypes.integer_to_bytes(
                    vartypes.int_to_integer_unsigned(
                        (self.code_start + 1) + pos + length))) + linebuf.read())
        # write back the remainder of the program
        self.truncate(rest)
        # update all next offsets by shifting them by the length of the added line
        self.update_line_dict(pos, afterpos, length, deleteable, beyond)
        if not empty:
            self.line_numbers[scanline] = pos
        self.last_stored = scanline

    def find_pos_line_dict(self, fromline, toline):
        """Find code positions for line range."""
        deleteable = [ num for num in self.line_numbers if num >= fromline and num <= toline ]
        beyond = [num for num in self.line_numbers if num > toline ]
        # find lowest number strictly above range
        afterpos = self.line_numbers[min(beyond)]
        # find lowest number within range
        try:
            startpos = self.line_numbers[min(deleteable)]
        except ValueError:
            startpos = afterpos
        return startpos, afterpos, deleteable, beyond

    def delete(self, fromline, toline):
        """Delete range of lines from stored program."""
        fromline = fromline if fromline is not None else min(self.line_numbers)
        toline = toline if toline is not None else 65535
        startpos, afterpos, deleteable, beyond = self.find_pos_line_dict(fromline, toline)
        if not deleteable:
            # no lines selected
            raise error.RunError(error.IFC)
        # do the delete
        self.bytecode.seek(afterpos)
        rest = self.bytecode.read()
        self.bytecode.seek(startpos)
        self.truncate(rest)
        # update line number dict
        self.update_line_dict(startpos, afterpos, 0, deleteable, beyond)

    def edit(self, screen, from_line, bytepos=None):
        """Output program line to console and position cursor."""
        if self.protected:
            screen.write(str(from_line)+'\r')
            raise error.RunError(error.IFC)
        # list line
        self.bytecode.seek(self.line_numbers[from_line]+1)
        _, output, textpos = self.tokeniser.detokenise_line(self.bytecode, bytepos)
        # no newline to avoid scrolling on line 24
        screen.list_line(str(output), newline=False)
        # find row, column position for textpos
        newlines, c = 0, 0
        pos_row, pos_col = 0, 0
        if not output:
            return
        for i, byte in enumerate(output):
            c += 1
            if chr(byte) == '\n' or c > screen.mode.width:
                newlines += 1
                c = 0
            if i == textpos:
                pos_row, pos_col = newlines, c
        if textpos > i:
            pos_row, pos_col = newlines, c + 1
        if bytepos:
            screen.set_pos(screen.current_row-newlines+pos_row, pos_col)
        else:
            screen.set_pos(screen.current_row-newlines, 1)

    def renum(self, screen, new_line, start_line, step):
        """Renumber stored program."""
        new_line = 10 if new_line is None else new_line
        start_line = 0 if start_line is None else start_line
        step = 10 if step is None else step
        # get a sorted list of line numbers
        keys = sorted([ k for k in self.line_numbers.keys() if k >= start_line])
        # assign the new numbers
        old_to_new = {}
        for old_line in keys:
            if old_line < 65535 and new_line > 65529:
                raise error.RunError(error.IFC)
            if old_line == 65536:
                break
            old_to_new[old_line] = new_line
            self.last_stored = new_line
            new_line += step
        # write the new numbers
        for old_line in old_to_new:
            self.bytecode.seek(self.line_numbers[old_line])
            # skip the \x00\xC0\xDE & overwrite line number
            self.bytecode.read(3)
            self.bytecode.write(str(vartypes.integer_to_bytes(vartypes.int_to_integer_unsigned(old_to_new[old_line]))))
        # write the indirect line numbers
        ins = self.bytecode
        ins.seek(0)
        while util.skip_to_read(ins, (tk.T_UINT,)) == tk.T_UINT:
            # get the old g number
            jumpnum = vartypes.integer_to_int_unsigned(vartypes.bytes_to_integer(ins.read(2)))
            # handle exception for ERROR GOTO
            if jumpnum == 0:
                pos = ins.tell()
                # skip line number token
                ins.seek(-3, 1)
                if util.backskip_white(ins) == tk.GOTO and util.backskip_white(ins) == tk.ERROR:
                    ins.seek(pos)
                    continue
                ins.seek(pos)
            try:
                newjump = old_to_new[jumpnum]
            except KeyError:
                # not redefined, exists in program?
                if jumpnum not in self.line_numbers:
                    linum = self.get_line_number(ins.tell()-1)
                    screen.write_line('Undefined line ' + str(jumpnum) + ' in ' + str(linum))
                newjump = jumpnum
            ins.seek(-2, 1)
            ins.write(str(vartypes.integer_to_bytes(vartypes.int_to_integer_unsigned(newjump))))
        # rebuild the line number dictionary
        new_lines = {}
        for old_line in old_to_new:
            new_lines[old_to_new[old_line]] = self.line_numbers[old_line]
            del self.line_numbers[old_line]
        self.line_numbers.update(new_lines)
        return old_to_new

    def load(self, g, rebuild_dict=True):
        """Load program from ascii, bytecode or protected stream."""
        self.erase()
        if g.filetype == 'B':
            # bytecode file
            self.bytecode.seek(1)
            self.bytecode.write(g.read())
        elif g.filetype == 'P':
            # protected file
            self.bytecode.seek(1)
            self.protected = self.allow_protect
            protect.unprotect(g, self.bytecode)
        elif g.filetype == 'A':
            # assume ASCII file
            # anything but numbers or whitespace: Direct Statement in File
            self.merge(g)
        else:
            logging.debug("Incorrect file type '%s' on LOAD", g.filetype)
        # rebuild line number dict and offsets
        if rebuild_dict and g.filetype != 'A':
            self.rebuild_line_dict()

    def merge(self, g):
        """Merge program from ascii or utf8 (if utf8_files is True) stream."""
        while True:
            line = g.read_line()
            if line is None:
                break
            linebuf = self.tokeniser.tokenise_line(line)
            if linebuf.read(1) == '\0':
                # line starts with a number, add to program memory; store_line seeks to 1 first
                self.store_line(linebuf)
            else:
                # we have read the :
                if util.skip_white(linebuf) not in tk.end_line:
                    raise error.RunError(error.DIRECT_STATEMENT_IN_FILE)

    def save(self, g):
        """Save the program to stream g in (A)scii, (B)ytecode or (P)rotected mode."""
        mode = g.filetype
        if self.protected and mode != 'P':
            raise error.RunError(error.IFC)
        current = self.bytecode.tell()
        # skip first \x00 in bytecode
        self.bytecode.seek(1)
        if mode == 'B':
            # binary bytecode mode
            g.write(self.bytecode.read())
        elif mode == 'P':
            # protected mode
            protect.protect(self.bytecode, g)
        else:
            # ascii mode
            while True:
                current_line, output, _ = self.tokeniser.detokenise_line(self.bytecode)
                if current_line == -1 or (current_line > self.max_list_line):
                    break
                g.write_line(str(output))
        self.bytecode.seek(current)

    def list_lines(self, from_line, to_line):
        """List line range."""
        if self.protected:
            # don't list protected files
            raise error.RunError(error.IFC)
        # 65529 is max insertable line number for GW-BASIC 3.23.
        # however, 65530-65535 are executed if present in tokenised form.
        # in GW-BASIC, 65530 appears in LIST, 65531 and above are hidden
        if to_line is None:
            to_line = self.max_list_line
        # sort by positions, not line numbers!
        listable = sorted([ self.line_numbers[num]
                                for num in self.line_numbers
                                if num >= from_line and num <= to_line ])
        lines = []
        for pos in listable:
            self.bytecode.seek(pos + 1)
            _, line, _ = self.tokeniser.detokenise_line(self.bytecode)
            lines.append(str(line))
        return lines

    def get_memory(self, offset):
        """Retrieve data from program code."""
        offset -= self.code_start
        code = self.bytecode.getvalue()
        try:
            return ord(code[offset])
        except IndexError:
            return -1

    def set_memory(self, offset, val):
        """Change program code."""
        if not self.allow_code_poke:
            logging.warning('Ignored POKE into program code')
        else:
            offset -= self.code_start
            loc = self.bytecode.tell()
            # move pointer to end
            self.bytecode.seek(0, 2)
            if offset > self.bytecode.tell():
                self.bytecode.write('\0' *
                            (offset-self.bytecode.tell()) + chr(val))
            else:
                self.bytecode.seek(offset)
                self.bytecode.write(chr(val))
            # restore program pointer
            self.bytecode.seek(loc)
            self.rebuild_line_dict()
