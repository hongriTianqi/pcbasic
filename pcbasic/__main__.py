#!/usr/bin/env python2

"""
PC-BASIC - GW-BASIC/BASICA/Cartridge BASIC compatible interpreter

(c) 2013, 2014, 2015, 2016 Rob Hagemans
This file is released under the GNU GPL version 3 or later.
"""

import os
import sys
import shutil
import logging
import platform
import subprocess
try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO
import traceback

import plat
import ansipipe
import config
import error

# video plugins
# these are unused but need to be initialised and packaged
import video_none
import video_ansi
import video_cli
import video_curses
import video_pygame
import video_sdl2

# audio plugins
import audio_none
import audio_beep
import audio_pygame
import audio_sdl2



def main():
    """Initialise and do requested operations"""
    try:
        # get settings and prepare logging
        settings = config.Settings()
        if settings.get('version'):
            # in version mode, print version and exit
            show_version(settings)
        elif settings.get('help'):
            # in help mode, print usage and exit
            show_usage(settings)
        elif settings.get('convert'):
            # in converter mode, convert and exit
            convert(settings)
        else:
            # otherwise, start an interpreter session
            start_basic(settings)
    except:
        raise
    finally:
        # clean up our temp dir if we made one
        if plat.temp_dir:
            shutil.rmtree(plat.temp_dir)
        # avoid sys.excepthook errors when piping output
        # http://stackoverflow.com/questions/7955138/addressing-sys-excepthook-error-in-bash-script
        try:
            sys.stdout.close()
        except:
            pass
        try:
            sys.stderr.close()
        except:
            pass

def convert(settings):
    """Perform file format conversion"""
    import interpreter
    # OS-specific stdin/stdout selection
    # no stdin/stdout access allowed on packaged apps in OSX
    if platform.system() == b'Darwin':
        has_stdio = False
    elif platform.system() == b'Windows':
        has_stdio = True
    else:
        try:
            sys.stdin.isatty()
            sys.stdout.isatty()
            has_stdio = True
        except AttributeError:
            has_stdio = False
    # set conversion output
    # first arg, if given, is mode; second arg, if given, is outfile
    mode = settings.get('convert')
    infile = (settings.get(0) or
              settings.get('run') or settings.get('load'))
    outfile = settings.get(1)
    # keep uppercase first letter
    mode = mode[0].upper() if mode else 'A'
    try:
        session = interpreter.Session(**settings.get_session_parameters())
    except Exception as e:
        logging.error('Unhandled exception\n%s' % traceback.format_exc())
    files = session.files
    internal_disk = session.devices.internal_disk
    prog = session.program
    # load & save in different format
    try:
        prog_infile = None
        if infile:
            prog_infile = files.open_native_or_basic(infile)
        elif has_stdio:
            # use StringIO buffer for seekability
            in_buffer = StringIO(sys.stdin.read())
            prog_infile = internal_disk.create_file_object(in_buffer, filetype='ABP', mode='I')
        if prog_infile:
            with prog_infile:
                prog.load(prog_infile, rebuild_dict=False)
        prog_outfile = None
        if outfile:
            # on save from command-line, use exact file name
            prog_outfile = internal_disk.create_file_object(open(outfile, 'wb'), filetype=mode, mode='O')
        elif has_stdio:
            prog_outfile = internal_disk.create_file_object(sys.stdout, filetype=mode, mode='O')
        if prog_outfile:
            with prog_outfile:
                prog.save(prog_outfile)
    except error.RunError as e:
        logging.error(e.message)
    except EnvironmentError as e:
        logging.error(str(e))

def start_basic(settings):
    """Start an interactive interpreter session"""
    import interface
    import interpreter
    interface_name = settings.get_interface()
    audio_params = settings.get_audio_parameters()
    video_params = settings.get_video_parameters()
    launch_params = settings.get_launch_parameters()
    session_params = settings.get_session_parameters()
    state_file = settings.get_state_file()
    try:
        with interpreter.SessionLauncher(session_params, state_file, **launch_params) as launcher:
            try:
                interface.run(
                        launcher.input_queue, launcher.video_queue,
                        launcher.tone_queue, launcher.message_queue,
                        interface_name, video_params, audio_params)
            except interface.InitFailed:
                logging.error('Failed to initialise interface.')
    except error.RunError as e:
        # only runtime errors that occur on interpreter launch are caught here
        # e.g. "File not Found" for --load parameter
        logging.error(e.message)
    except Exception:
        logging.error('Unhandled exception\n%s' % traceback.format_exc())

def show_usage(settings):
    """Show usage description"""
    with open(os.path.join(plat.info_dir, 'usage.txt')) as f:
        for line in f:
            sys.stdout.write(line)

def show_version(settings):
    """Show version with optional debugging details"""
    sys.stdout.write(plat.version + '\n')
    if not settings.get('debug'):
        return
    logging.info('\nPLATFORM')
    logging.info('os: %s %s %s', platform.system(), platform.processor(), platform.version())
    logging.info('python: %s %s', sys.version.replace('\n',''), ' '.join(platform.architecture()))
    logging.info('\nMODULES')
    # try numpy before pygame to avoid strange ImportError on FreeBSD
    modules = ('numpy', 'win32api', 'sdl2', 'pygame', 'curses', 'pexpect', 'serial', 'parallel')
    for module in modules:
        try:
            m = __import__(module)
        except ImportError:
            logging.info('%s: --', module)
        else:
            for version_attr in ('__version__', 'version', 'VERSION'):
                try:
                    version = getattr(m, version_attr)
                    logging.info('%s: %s', module, version)
                    break
                except AttributeError:
                    pass
            else:
                logging.info('available\n')
    if platform.system() != 'Windows':
        logging.info('\nEXTERNAL TOOLS')
        tools = ('lpr', 'paps', 'beep', 'xclip', 'xsel', 'pbcopy', 'pbpaste')
        for tool in tools:
            try:
                location = subprocess.check_output('command -v %s' % tool, shell=True).replace('\n','')
                logging.info('%s: %s', tool, location)
            except Exception as e:
                logging.info('%s: --', tool)


if __name__ == "__main__":
    main()
