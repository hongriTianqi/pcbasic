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
try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO
import traceback
import subprocess

import plat
import ansipipe
import printer

# declare to avoid pylint errors
config = None

def main():
    """ Initialise and do requested operations. """
    # make imported modules available in main module
    global config
    try:
        import config
        if plat.system == 'Android':
            # resume from existing directory (or clear it if we're not resuming)
            if not config.get('resume') and os.path.exists(plat.temp_dir):
                shutil.rmtree(plat.temp_dir)
            if not os.path.exists(plat.temp_dir):
                os.mkdir(plat.temp_dir)
        # set up the logging system
        prepare_logging()
        if config.get('version'):
            # in version mode, print version and exit
            sys.stdout.write(plat.version + '\n')
            if config.get('debug'):
                import debug
                debug.details()
        elif config.get('help'):
            # in help mode, print usage and exit
            with open(os.path.join(plat.info_dir, 'usage.txt')) as f:
                for line in f:
                    sys.stdout.write(line)
        elif config.get('convert'):
            # in converter mode, convert and exit
            convert()
        else:
            # otherwise, go into BASIC
            start_basic()
    except error.Reset:
        if plat.system == 'Android':
            shutil.rmtree(plat.temp_dir)
    finally:
        try:
            printer.wait()
            # clean up our temp dir if we made one
            if plat.temp_dir and plat.system != 'Android':
                shutil.rmtree(plat.temp_dir)
        except NameError:
            pass
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

def prepare_logging():
    """ Set up the logging system. """
    logfile = config.get('logfile')
    if config.get('version') or config.get('help'):
        formatstr = '%(message)s'
        loglevel = logging.INFO
    else:
        # logging setup before we import modules and may need to log errors
        formatstr = '%(levelname)s: %(message)s'
        if config.get('debug'):
            loglevel = logging.DEBUG
        else:
            loglevel = logging.INFO
    logging.basicConfig(format=formatstr, level=loglevel, filename=logfile)

def convert():
    """ Perform file format conversion. """
    import program
    import disk
    import error
    # set conversion output
    # first arg, if given, is mode; second arg, if given, is outfile
    mode = config.get('convert')
    infile = (config.get(0) or
              config.get('run') or config.get('load'))
    outfile = config.get(1)
    # keep uppercase first letter
    mode = mode[0].upper() if mode else 'A'
    # load & save in different format
    try:
        prog_infile = None
        if infile:
            prog_infile = disk.open_native_or_dos_filename(infile)
        elif plat.has_stdin:
            # use StringIO buffer for seekability
            in_buffer = StringIO(sys.stdin.read())
            prog_infile = disk.create_file_object(in_buffer, filetype='ABP', mode='I')
        if prog_infile:
            with prog_infile:
                program.load(prog_infile)
        prog_outfile = None
        if outfile:
            # on save from command-line, use exact file name
            prog_outfile = disk.create_file_object(open(outfile, 'wb'), filetype=mode, mode='O')
        elif plat.has_stdout:
            prog_outfile = disk.create_file_object(sys.stdout, filetype=mode, mode='O')
        if prog_outfile:
            with prog_outfile:
                program.save(prog_outfile)
    except error.RunError as e:
        logging.error(e.message)
    except EnvironmentError as e:
        logging.error(str(e))

def start_basic():
    """ Start an interactive interpreter session. """
    import interpreter
    import error
    import state
    import audio
    import video
    exit_error = ''
    try:
        # choose the video and sound backends
        interface = config.get('interface') or 'graphical'
        init_video_plugin(interface)
        init_audio_plugin('none' if config.get('nosound') else interface)
        # resume from saved emulator state if requested and available
        if config.get('resume') and state.load():
            interpreter.resume()
        else:
            # greet, load and start the interpreter
            interpreter.start()
    except KeyboardInterrupt:
        if config.get('debug'):
            raise
    except error.Reset:
        # delete state if resetting
        state.delete()
        raise
    except error.RunError as e:
        exit_error = e.message
    except Exception as e:
        exit_error = "Unhandled exception\n%s" % traceback.format_exc()
    finally:
        try:
            audio.close()
        except (NameError, AttributeError) as e:
            logging.debug('Error on closing audio: %s', e)
        try:
            # fix the terminal on exit (important for ANSI terminals)
            video.close()
        except (NameError, AttributeError) as e:
            logging.debug('Error on closing video: %s', e)
        if exit_error:
            logging.error(exit_error)


###############################################################################
# video plugins

# these are unused but need to be initialised and packaged
import video
import video_none
import video_ansi
import video_cli
import video_curses
import video_pygame
import video_sdl2

video_backends = {
    # interface_name: video_plugin_name, fallback, warn_on_fallback
    'none': (('none',), None),
    'cli': (('cli',), 'none'),
    'text': (('curses', 'ansi'), 'cli'),
    'graphical':  (('sdl2', 'pygame',), 'text'),
    # force a particular plugin to be used
    'ansi': (('ansi',), None),
    'curses': (('curses',), None),
    'pygame': (('pygame',), None),
    'sdl2': (('sdl2',), None),
    }

# create the window icon
icon_hex = '00003CE066606666666C6678666C3CE67F007F007F007F007F007F007F000000'

def init_video_plugin(interface_name):
    """ Find and initialise video plugin for given interface. """
    import typeface
    import state
    # set state.console_state.codepage
    import unicodepage
    # needed to set console_state.screen state before setting up video plugin
    import display
    icon = typeface.Font(16, {'icon': icon_hex.decode('hex')}
                                ).build_glyph('icon', 16, 16, False, False)
    while True:
        # select interface
        names, fallback = video_backends[interface_name]
        for video_name in names:
            if video.init(video_name,
                    force_display_size=config.get('dimensions'),
                    aspect=config.get('aspect'),
                    border_width=config.get('border'),
                    force_native_pixel=(config.get('scaling') == 'native'),
                    fullscreen=config.get('fullscreen'),
                    smooth=(config.get('scaling') == 'smooth'),
                    nokill=config.get('nokill'),
                    altgr=config.get('altgr'),
                    caption=config.get('caption'),
                    composite_monitor=(config.get('monitor') == 'composite'),
                    composite_card=config.get('video'),
                    copy_paste=config.get('copy-paste'),
                    pen=config.get('pen'),
                    icon=icon,
                    initial_mode=state.console_state.screen.mode,
                    codepage=state.console_state.codepage):
                return interface_name
            else:
                logging.debug('Could not initialise %s plugin.', video_name)
        if fallback:
            logging.info('Could not initialise %s interface. Falling back to %s interface.', interface_name, fallback)
            interface_name = fallback
        else:
            logging.error('Failed to initialise interface.')
            raise error.Exit()


###############################################################################
# audio plugins

import audio
import audio_none
import audio_beep
import audio_pygame
import audio_sdl2

audio_backends = {
    # interface_name: plugin_name, fallback, warn_on_fallback
    'none': ('none',),
    'cli': ('beep', 'none'),
    'text': ('beep', 'none'),
    'graphical': ('sdl2', 'pygame', 'beep', 'none'),
    'ansi': ('none',),
    'curses': ('none',),
    'pygame': ('pygame', 'none'),
    'sdl2': ('sdl2', 'none'),
    }

def init_audio_plugin(interface_name):
    """ Find and initialise audio plugin for given interface. """
    names = audio_backends[interface_name]
    for audio_name in names:
        if audio.init(audio_name):
            return interface_name
        logging.debug('Could not initialise %s plugin.', audio_name)
    logging.error('Null sound plugin malfunction. Could not initialise interface.')
    raise error.Exit()


if __name__ == "__main__":
    main()
