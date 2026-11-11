# Phasma

Phasma is a paranormal investigation station built on an RTL-SDR receiver. It measures the radio spectrum directly and records what it measures: an RF field meter, a spirit box that sweeps real broadcast radio, and a timestamped session log that keeps every reading and every recording for review afterward.

The name is the Greek phasma: an apparition, and in modern Greek the everyday word for spectrum. The instrument reads the spectrum. What it is watching for is the other meaning.

Phasma is a separate program from Eidolon. Eidolon is the pocket kit built for the Flipper Zero; Phasma is the base station, running on a computer with the receiver attached. They share no code and neither one depends on the other.

## Instruments

The field meter reads real RF power anywhere the receiver can tune, HF through 1.7 GHz with an RTL-SDR Blog V4, against a baseline taken in the room, with the peak held and every excursion written to the log.

The spirit box sweeps the FM and AM broadcast bands the way an SB7 does, except the audio is genuine: real demodulated radio, real static, and real fragments of voice and music as the sweep crosses stations. Nothing is synthesized. Each session is recorded to disk as audio, so anything heard in the moment can be played back and checked afterward.

The session log is the spine of the program. Every instrument event is timestamped and written down as it happens, and the audio recordings are filed with the session they belong to.

A word and radar layer is driven by the measured spectrum, and the program labels it for what it is: theater fed by real measurements, not a measurement itself.

## Hardware and honesty

Phasma needs an RTL-SDR Blog V4 (the design targets the RTL2832U family) and a computer to run it. Nothing else is required. The proximity and infrared instruments of the pocket kit are not part of Phasma, because a receiver has no analog input and no infrared eye, and Phasma does not pretend otherwise.

Every number Phasma shows comes from the receiver. Where a display is theater rather than measurement, the program says so on its face.

## State

Phasma is at founding. The instrument designs are settled, and construction begins once Eidolon has shipped on the Flipper.

## License

AGPL-3.0. See LICENSE.
