import multiprocessing.context
import nacl.secret
import queue
import struct
import threading
from concurrent.futures import Future
from typing import Dict, List, Optional, Tuple, Union, Any, TYPE_CHECKING

from .opus import Decoder
from .sink import SILENT_FRAME, AudioFrame, RawAudioData, RTCPPacket, get_audio_packet

from multiprocessing.connection import Connection
import os



__all__ = ("AudioProcessPool",)

_mp_ctx: multiprocessing.context.SpawnContext = multiprocessing.get_context("spawn")

class AudioUnpacker(_mp_ctx.Process):
    def __init__(self, pipe: Connection, patience : Optional[float], **kwargs):
        super().__init__(daemon=True, **kwargs)
        self.pipe = pipe
        self.patience = patience
        self.secret_key: Optional[List[int]] = None
        self.decoders: Dict[int, Decoder] = {}

    def run(self) -> None:
        print(f"Run method running in new process: {self.pipe} {self.patience} (PID: {os.getpid()})")

        while True:
            try:
                if not self.pipe.poll(self.patience):
                    print(f"Closing Pipe")
                    self.pipe.close()
                    return

                data, decode, mode, secret_key = self.pipe.recv()
                if secret_key is not None:
                    self.secret_key = secret_key

                packet = self.unpack_audio_packet(data, mode, decode)
                if isinstance(packet, RTCPPacket):
                    # enum not picklable
                    packet.pt = packet.pt.value  # type: ignore

                self.pipe.send(packet)
            except EOFError:
                print(f"Pipe Closed - Terminating Process")
                # the pipe was closed for whatever reason so just terminate
                return
            except BaseException as exc:
                print(f"Processing Audio Exception: {exc}")
                self.pipe.send(exc)
                return

    def _decrypt_xsalsa20_poly1305(self, header, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.secret_key))  # type: ignore

        nonce = bytearray(24)
        nonce[:12] = header

        return self.strip_header_ext(box.decrypt(bytes(data), bytes(nonce)))

    def _decrypt_xsalsa20_poly1305_suffix(self, header, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.secret_key))  # type: ignore

        nonce_size = nacl.secret.SecretBox.NONCE_SIZE
        nonce = data[-nonce_size:]

        return self.strip_header_ext(box.decrypt(bytes(data[:-nonce_size]), nonce))

    def _decrypt_xsalsa20_poly1305_lite(self, header, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.secret_key))  # type: ignore

        nonce = bytearray(24)
        nonce[:4] = data[-4:]
        data = data[:-4]

        return self.strip_header_ext(box.decrypt(bytes(data), bytes(nonce)))

    def _decrypt_rtp_aead_xchacha20_poly1305_rtpsize(self, packet: RTPPacket) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.secret_key))  # type: ignore
        packet.adjust_rtpsize()

        nonce = bytearray(24)
        nonce[:4] = packet.nonce
        voice_data = packet.data

        # Blob vomit
        assert isinstance(box, nacl.secret.Aead)
        result = box.decrypt(bytes(voice_data), bytes(packet.header), bytes(nonce))


        if packet.extended:
            offset = packet.update_ext_headers(result)
            result = result[offset:]

        return result

    def _decrypt_rtcp_aead_xchacha20_poly1305_rtpsize(self, data: bytes) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.secret_key))  # type: ignore
    
        nonce = bytearray(24)
        nonce[:4] = data[-4:]
        header = data[:8]

        assert isinstance(box, nacl.secret.Aead)

        result = box.decrypt(data[8:-4], bytes(header), bytes(nonce))

        return header + result

    @staticmethod
    def strip_header_ext(data: bytes) -> bytes:
        if data[0] == 0xBE and data[1] == 0xDE and len(data) > 4:
            _, length = struct.unpack_from(">HH", data)
            offset = 4 + length * 4
            data = data[offset:]
        return data

    def unpack_audio_packet(self, data: bytes, mode: str, decode: bool) -> Union[RTCPPacket, AudioFrame]:
        packet = get_audio_packet(data, getattr(self, "_decrypt_" + mode))

        if not isinstance(packet, RawAudioData):  # is RTCP packet
            return packet

        if decode and packet.audio != SILENT_FRAME:
            if packet.ssrc not in self.decoders:
                self.decoders[packet.ssrc] = Decoder()
            return AudioFrame(self.decoders[packet.ssrc].decode(packet.audio), packet, None)  # type: ignore

        return AudioFrame(packet.audio, packet, None)


class ProcessConnection:
    def __init__(self, conn: Connection, process: AudioUnpacker):
        self.conn = conn
        self.process = process

    def send(self, *, data):
        self.conn.send(data)

    def recv(self) -> Any:
        return self.conn.recv()

    def close(self):
        self.conn.close()

    def terminate(self):
        self.process.terminate()


class AudioProcessPool:
    """Process pool for processing audio packets received from voice channels.

    Accepts submissions of audio frames, which are sent to a child process for processing.
    Audio is submitted with a specified process to use. If the specified process does not exist,
    it is created. A separate thread is notified via a queue that it should be expecting to receive
    processed audio from that process.

    Parameters
    ----------
    max_processes: :class:`int`
        The audio processing pool will distribute audio processing across
        this number of processes.
    wait_timeout: Optional[:class:`float`]
        Decides how long the looping thread (explained above) waits to receive a result before finishing.
        Default is 3. None means it will never finish via timeout.
    process_patience: Optional[:class:`float`]
        Decides how long a process will wait to receive audio until finishing.
        Default is None, meaning a process will never finish itself via timeout.

    Raises
    ------
    ValueError
        max_processes must be greater than 0 or wait_timeout cannot be negative
    """

    def __init__(
        self, max_processes: int, *, wait_timeout: Optional[float] = 3, process_patience: Optional[float] = None
    ):
        if max_processes <= 0:
            raise ValueError("max_processes must be greater than 0")
        if wait_timeout < 0:
            raise ValueError("wait_timeout cannot be a negative number")

        self.max_processes: int = max_processes
        self.wait_timeout: Optional[float] = wait_timeout
        self.process_patience: Optional[float] = process_patience
        self._processes: Dict[int, ProcessConnection] = {}
        self._wait_queue: queue.Queue[Tuple[int, Future[Any]]] = queue.Queue()

        self._wait_loop_running: threading.Event = threading.Event()
        # used for interacting with self._processes safely
        self._lock: threading.Lock = threading.Lock()

    def submit(self, data: bytes, n_p: int, decode: bool, mode: str, secret_key: List[int]) -> Future:
        """Submit raw audio data for processing in a specific child process.

        Parameters
        ----------
        data: :class:`bytes`
            Audio frame to process
        n_p: :class:`int`
            Process index to send audio frame to
        decode: :class:`bool`
            Whether to perform decoding on the audio frame
        mode: :class:`str`
            Decryption mode
        secret_key: List[:class:`str`]
            Secret key used for nacl decryption

        Returns
        -------
        :class:`Future`
            A future that resolves when the process returns the processed audio frame or an error
        """
        with self._lock:
            if n_p >= self.max_processes:
                raise ValueError(f"n_p must be less than the maximum processes ({self.max_processes})")

            if n_p not in self._processes:
                self._spawn_process(n_p)

            future = Future()
            
            try:
                self._processes[n_p].send(data=(data, decode, mode, secret_key))
            except Exception as e:
                future.set_exception(e)
                return future
            
            # notify _recv_loop that it should expect to receive audio from this process
            self._wait_queue.put((n_p, future))
            self._start_recv_loop()
        
        return future

    def cleanup_processes(self):
        """Close all :class:`Connection` pipes and terminate all processes."""
        with self._lock:
            for process in self._processes.values():
                # close pipe and terminate process
                process.close()
                process.terminate()
            self._processes = {}

    def _spawn_process(self, n_p) -> None:
        # the function calling this one must have acquired self._lock
        submit_conn, recv_conn = _mp_ctx.Pipe(duplex=True)
        process = AudioUnpacker(pipe=recv_conn, patience=self.process_patience)
        process.start()
        self._processes[n_p] = ProcessConnection(submit_conn, process)

    def _start_recv_loop(self) -> None:
        # check if _recv_loop is running; if not, start running it in a new thread
        if not self._wait_loop_running.is_set():
            threading.Thread(target=self._recv_loop).start()

    def _recv_loop(self) -> None:
        try:
            self._wait_loop_running.set()

            while True:
                try:
                    n_p, future = self._wait_queue.get(timeout=self.wait_timeout)
                except queue.Empty:
                    break

                try:
                    ret = self._processes[n_p].recv()
                except EOFError as ex:
                    ret = ex
                    # process probably terminated, but call to terminate is made just in case
                    with self._lock:
                        self._processes[n_p].terminate()
                        self._processes.pop(n_p)

                if isinstance(ret, BaseException):
                    future.set_exception(ret)
                else:
                    future.set_result(ret)
        finally:
            self._wait_loop_running.clear()
