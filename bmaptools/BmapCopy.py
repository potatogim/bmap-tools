""" This module implements copying of images with bmap and provides the
following API.
  1. BmapCopy class - implements copying to any kind of file, be that a block
     device or a regular file.
  2. BmapBdevCopy class - based on BmapCopy and specializes on copying to block
     devices. It does some more sanity checks and some block device performance
     tuning.

The bmap file is an XML file which contains a list of mapped blocks of the
image. Mapped blocks are the blocks which have disk sectors associated with
them, as opposed to holes, which are blocks with no associated disk sectors. In
other words, the image is considered to be a sparse file, and bmap basically
contains a list of mapped blocks of this sparse file. The bmap additionally
contains some useful information like block size (usually 4KiB), image size,
mapped blocks count, etc.

The bmap is used for copying the image to a block device or to a regualr file.
The idea is that we copy quickly with bmap because we copy only mapped blocks
and ignore the holes, because they are useless. And if the image is generated
properly (starting with a huge hole and writing all the data), it usually
contains only little mapped blocks, comparing to the overall image size. And
such an image compresses very well (because holes are read as all zeroes), so
it is benefitial to destribute them as compressed files along with the bmap.

Here is an example. Suppose you have a 4GiB image which contains only 100MiB of
user data and you need to flash it to a slow USB stick. With bmap you end up
copying only a little bit more than 100MiB of data from the image to the USB
stick (namely, you copy only mapped blocks). This is a lot faster than copying
all 4GiB of data. We say that it is a bit more than 100MiB because things like
file-system meta-data (inode tables, superblocks, etc), partition table, etc
also contribute to the mapped blocks and are also copied. """

# Disable the "Too many instance attributes" pylint recommendation (R0902)
# pylint: disable=R0902

import os
import stat
import sys
import hashlib
import Queue
import thread
from xml.etree import ElementTree
from bmaptools.BmapHelpers import human_size

# A list of supported image formats
SUPPORTED_IMAGE_FORMATS = ('bz2', 'gz', 'tar.gz', 'tgz', 'tar.bz2')

# The highest supported bmap format version
SUPPORTED_BMAP_VERSION = 1

class Error(Exception):
    """ A class for exceptions generated by the 'BmapCopy' module. We currently
    support only one type of exceptions, and we basically throw human-readable
    problem description in case of errors. """
    pass

class BmapCopy:
    """ This class implements the bmap-based copying functionality. To copy an
    image with bmap you should create an instance of this class, which requires
    the following:

    * full path or a file-like object of the image to copy
    * full path or a file-like object of the destination file copy the image to
    * full path or a file-like object of the bmap file (optional)

    Although the main purpose of this class is to use bmap, the bmap is not
    required, and if it was not provided then the entire image will be copied
    to the destination file.

    The image file may either be an uncompressed raw image or a compressed
    image. Compression type is defined by the image file extension.  Supported
    types are listed by 'SUPPORTED_IMAGE_FORMATS'.

    IMPORTANT: if the image is given as a file-like object, the compression
    type recognition is not performed - the file-like object's 'read()' method
    is used directly instead.

    Once an instance of 'BmapCopy' is created, all the 'bmap_*' attributes are
    initialized and available. They are read from the bmap.

    However, if bmap was not provided, this is not always the case and some of
    the 'bmap_*' attributes are not initialize by the class constructore.
    Instead, they are initialized only in the 'copy()' method. The reason for
    this is that when bmap is absent, 'BmapCopy' uses sensible fall-back values
    for the 'bmap_*' attributes assuming the entire image is "mapped". And if
    the image is compressed, it annot easily find out the image size. Thus,
    this is postponed until the 'copy()' method decompresses the image for the
    first time.

    The 'copy()' method implements the copying. You may choose whether to
    verify the SHA1 checksum while copying or not.  Note, this is done only in
    case of bmap-based copying and only if bmap contains the SHA1 checksums
    (e.g., bmap version 1.0 did not have SHA1 checksums).

    You may choose whether to synchronize the destination file after writing or
    not. To explicitly synchronize it, use the 'sync()' method.

    This class supports all the bmap format versions up version
    'SUPPORTED_BMAP_VERSION'. """

    def _initialize_sizes(self, image_size):
        """ This function is only used when the there is no bmap. It
        initializes attributes like 'blocks_cnt', 'mapped_cnt', etc. Normally,
        the values are read from the bmap file, but in this case they are just
        set to something reasonable. """

        self.image_size = image_size
        self.image_size_human = human_size(image_size)
        self.blocks_cnt = self.image_size + self.block_size - 1
        self.blocks_cnt /= self.block_size
        self.mapped_cnt = self.blocks_cnt
        self.mapped_size = self.image_size
        self.mapped_size_human = self.image_size_human


    def _parse_bmap(self):
        """ Parse the bmap file and initialize the 'bmap_*' attributes. """

        bmap_pos = self._f_bmap.tell()
        self._f_bmap.seek(0)

        try:
            self._xml = ElementTree.parse(self._f_bmap)
        except  ElementTree.ParseError as err:
            raise Error("cannot parse the bmap file '%s' which should be a " \
                        "proper XML file: %s" % (self._bmap_path, err))

        xml = self._xml
        self.bmap_version = str(xml.getroot().attrib.get('version'))

        # Make sure we support this version
        major = int(self.bmap_version.split('.', 1)[0])
        if major > SUPPORTED_BMAP_VERSION:
            raise Error("only bmap format version up to %d is supported, " \
                        "version %d is not supported" \
                        % (SUPPORTED_BMAP_VERSION, major))

        # Fetch interesting data from the bmap XML file
        self.block_size = int(xml.find("BlockSize").text.strip())
        self.blocks_cnt = int(xml.find("BlocksCount").text.strip())
        self.mapped_cnt = int(xml.find("MappedBlocksCount").text.strip())
        self.image_size = self.blocks_cnt * self.block_size
        self.image_size_human = human_size(self.image_size)
        self.mapped_size = self.mapped_cnt * self.block_size
        self.mapped_size_human = human_size(self.mapped_size)
        self.mapped_percent = (self.mapped_cnt * 100.0) / self.blocks_cnt

        self._f_bmap.seek(bmap_pos)

    def _open_image_file(self):
        """ Open the image file which may be compressed or not. The compression
        type is recognized by the file extension. Supported types are defined
        by 'SUPPORTED_IMAGE_FORMATS'. """

        try:
            is_regular_file = stat.S_ISREG(os.stat(self._image_path).st_mode)
        except OSError as err:
            raise Error("cannot access image file '%s': %s" \
                        % (self._image_path, err.strerror))

        if not is_regular_file:
            raise Error("image file '%s' is not a regular file" \
                        % self._image_path)

        try:
            if self._image_path.endswith('.tar.gz') \
               or self._image_path.endswith('.tar.bz2') \
               or self._image_path.endswith('.tgz'):
                import tarfile

                tar = tarfile.open(self._image_path, 'r')
                # The tarball is supposed to contain only one single member
                members = tar.getnames()
                if len(members) > 1:
                    raise Error("the image tarball '%s' contains more than " \
                                "one file" % self._image_path)
                elif len(members) == 0:
                    raise Error("the image tarball '%s' is empty (no files)" \
                                % self._image_path)
                self._f_image = tar.extractfile(members[0])
            if self._image_path.endswith('.gz'):
                import gzip
                self._f_image = gzip.GzipFile(self._image_path, 'rb')
            elif self._image_path.endswith('.bz2'):
                import bz2
                self._f_image = bz2.BZ2File(self._image_path, 'rb')
            else:
                self._image_is_compressed = False
                self._f_image = open(self._image_path, 'rb')
        except IOError as err:
            raise Error("cannot open image file '%s': %s" \
                        % (self._image_path, err))

        self._f_image_needs_close = True

    def _open_destination_file(self):
        """ Open the destination file. """

        try:
            self._f_dest = open(self._dest_path, 'w')
        except IOError as err:
            raise Error("cannot open destination file '%s': %s" \
                        % (self._dest_path, err))

        self._f_dest_needs_close = True

    def _open_bmap_file(self):
        """ Open the bmap file. """

        try:
            self._f_bmap = open(self._bmap_path, 'r')
        except IOError as err:
            raise Error("cannot open bmap file '%s': %s" \
                        % (self._bmap_path, err.strerror))

        self._f_bmap_needs_close = True

    def __init__(self, image, dest, bmap = None):
        """ The class constructor. The parameters are:
            image - full path or file object of the image which should be copied
            dest  - full path or file-like object of the destination file to
                    copy the image to
            bmap  - full path or file-like object of the bmap file to use for
                    copying """

        self._xml = None
        self._image_is_compressed = True

        self._dest_fsync_watermark = None
        self._batch_blocks = None
        self._batch_queue = None
        self._batch_bytes = 1024 * 1024
        self._batch_queue_len = 2

        self.bmap_version = None
        self.block_size = None
        self.blocks_cnt = None
        self.mapped_cnt = None
        self.image_size = None
        self.image_size_human = None
        self.mapped_size = None
        self.mapped_size_human = None
        self.mapped_percent = None

        self._f_dest_needs_close = False
        self._f_image_needs_close = False
        self._f_bmap_needs_close = False

        self._f_dest = None
        self._f_image = None
        self._f_bmap = None

        self._dest_path  = None
        self._image_path = None
        self._bmap_path = None

        if hasattr(dest, "write"):
            self._f_dest = dest
            self._dest_path = dest.name
        else:
            self._dest_path = dest
            self._open_destination_file()

        if hasattr(image, "read"):
            self._f_image = image
            self._image_path = image.name
        else:
            self._image_path = image
            self._open_image_file()

        if bmap:
            if hasattr(bmap, "read"):
                self._f_bmap = bmap
                self._bmap_path = bmap.name
            else:
                self._bmap_path = bmap
                self._open_bmap_file()
            self._parse_bmap()
        else:
            # There is no bmap. Initialize user-visible attributes to something
            # sensible with an assumption that we just have all blocks mapped.
            self.bmap_version = 0
            self.block_size = 4096
            self.mapped_percent = 100

            # We can initialize size-related attributes only if we the image is
            # uncompressed.
            if not self._image_is_compressed:
                image_size = os.fstat(self._f_image.fileno()).st_size
                self._initialize_sizes(image_size)

        self._batch_blocks = self._batch_bytes / self.block_size

    def __del__(self):
        """ The class destructor which closes the opened files. """

        if self._f_image_needs_close:
            self._f_image.close()
        if self._f_dest_needs_close:
            self._f_dest.close()
        if self._f_bmap_needs_close:
            self._f_bmap.close()

    def _get_block_ranges(self):
        """ This is a helper iterator that parses the bmap XML file and for
        each block range in the XML file it generates a
        ('first', 'last', 'sha1') triplet, where:
          * 'first' is the first block of the range;
          * 'last' is the last block of the range;
          * 'sha1' is the SHA1 checksum of the range ('None' is used if it is
            missing.

        If there is no bmap file, the iterator just generate a single range for
        entire image file. If the image size is unknown (the image is
        compressed), the iterator infinitely generates continuous ranges of
        size '_batch_blocks'. """

        if not self._f_bmap:
            # We do not have the bmap, generate a tuple with all blocks
            if self.blocks_cnt:
                yield (0, self.blocks_cnt - 1, None)
            else:
                # We do not know image size, keep generate tuple with many
                # blocks infinitely
                first = 0
                while True:
                    yield (first, first + self._batch_blocks - 1, None)
                    first += self._batch_blocks
            return

        # We have the bmap, just read it ang generate block ranges
        xml = self._xml
        xml_bmap = xml.find("BlockMap")

        for xml_element in xml_bmap.findall("Range"):
            blocks_range = xml_element.text.strip()
            # The range of blocks has the "X - Y" format, or it can be just "X"
            # in old bmap format versions. First, split the blocks range string
            # and strip white-spaces.
            split = [x.strip() for x in blocks_range.split('-', 1)]

            first = int(split[0])
            if len(split) > 1:
                last = int(split[1])
                if first > last:
                    raise Error("bad range (first > last): '%s'" % blocks_range)
            else:
                last = first

            if 'sha1' in xml_element.attrib:
                sha1 = xml_element.attrib['sha1']
            else:
                sha1 = None

            yield (first, last, sha1)

    def _get_batches(self, first, last):
        """ This is a helper iterator which splits block ranges from the bmap
        file to smaller batches. Indeed, we cannot read and write entire block
        ranges from the image file, because a range can be very large. So we
        perform the I/O in batches. Batch size is defined by the
        '_batch_blocks' attribute. Thus, for each (first, last) block range,
        the iterator returns smaller (start, end, length) batch ranges, where:
          * 'start' is the starting batch block number;
          * 'last' is the ending batch block numger;
          * 'length' is the batch length in blocks (same as
             'end' - 'start' + 1). """

        batch_blocks = self._batch_blocks

        while first + batch_blocks - 1 <= last:
            yield (first, first + batch_blocks - 1, batch_blocks)
            first += batch_blocks

        batch_blocks = last - first + 1
        if batch_blocks:
            yield (first, first + batch_blocks - 1, batch_blocks)

    def _get_data(self, verify):
        """ This is an iterator which reads the image file in '_batch_blocks'
        chunks and returns ('start', 'end', 'length', 'buf) tuples, where:
          * 'start' is the starting block number of the batch;
          * 'end' is the last block of the batch;
          * 'length' is batch length (same as 'end' - 'start' + 1);
          * 'buf' a buffer containing the batch data. """

        try:
            for (first, last, sha1) in self._get_block_ranges():
                if verify and sha1:
                    hash_obj = hashlib.new('sha1')

                self._f_image.seek(first * self.block_size)

                iterator = self._get_batches(first, last)
                for (start, end, length) in iterator:
                    try:
                        buf = self._f_image.read(length * self.block_size)
                    except IOError as err:
                        raise Error("error while reading blocks %d-%d of the " \
                                    "image file '%s': %s" \
                                    % (start, end, self._image_path, err))

                    if not buf:
                        self._batch_queue.put(None)
                        return

                    if verify and sha1:
                        hash_obj.update(buf)

                    length = len(buf) + self.block_size - 1
                    length /= self.block_size
                    end = start + length - 1
                    self._batch_queue.put(("range", start, end, length, buf))

                if verify and sha1 and hash_obj.hexdigest() != sha1:
                    raise Error("checksum mismatch for blocks range %d-%d: " \
                                "calculated %s, should be %s" \
                                % (first, last, hash_obj.hexdigest(), sha1))
        # Silence pylint warning about catching too general exception
        # pylint: disable=W0703
        except Exception:
            # pylint: enable=W0703
            # In case of any exception - just pass it to the main thread
            # through the queue.
            self._batch_queue.put(("error", sys.exc_info()))

        self._batch_queue.put(None)

    def copy(self, sync = True, verify = True):
        """ Copy the image to the destination file using bmap. The sync
        argument defines whether the destination file has to be synchronized
        upon return.  The 'verify' argument defines whether the SHA1 checksum
        has to be verified while copying. """

        # Save file positions in order to restore them at the end
        image_pos = self._f_image.tell()
        dest_pos = self._f_dest.tell()
        if self._f_bmap:
            bmap_pos = self._f_bmap.tell()

        # Create the queue for block batches and start the reader thread, which
        # will read the image in batches and put the results to '_batch_queue'.
        self._batch_queue = Queue.Queue(self._batch_queue_len)
        thread.start_new_thread(self._get_data, (verify, ))

        blocks_written = 0
        fsync_last = 0

        # Read the image in '_batch_blocks' chunks and write them to the
        # destination file
        while True:
            batch = self._batch_queue.get()
            if batch is None:
                # No more data, the image is written
                break
            elif batch[0] == "error":
                # The reader thread encountered an error and passed us the
                # exception.
                exc_info = batch[1]
                raise exc_info[0], exc_info[1], exc_info[2]

            (start, end, length, buf) = batch[1:5]

            assert len(buf) <= length * self.block_size

            self._f_dest.seek(start * self.block_size)

            # Synchronize the destination file if we reached the watermark
            if self._dest_fsync_watermark:
                if blocks_written >= fsync_last + self._dest_fsync_watermark:
                    fsync_last = blocks_written
                    self.sync()

            try:
                self._f_dest.write(buf)
            except IOError as err:
                raise Error("error while writing blocks %d-%d of '%s': %s" \
                            % (start, end, self._dest_path, err))

            self._batch_queue.task_done()
            blocks_written += length

        if not self.image_size:
            # The image size was unknow up until now, probably because this is
            # a compressed image. Initialize the corresponding class attributes
            # now, when we know the size.
            self._initialize_sizes(blocks_written * self.block_size)

        # This is just a sanity check - we should have written exactly
        # 'mapped_cnt' blocks.
        if blocks_written != self.mapped_cnt:
            raise Error("wrote %u blocks, but should have %u - inconsistent " \
                       "bmap file" % (blocks_written, self.mapped_cnt))

        try:
            self._f_dest.flush()
        except IOError as err:
            raise Error("cannot flush '%s': %s" % (self._dest_path, err))

        if sync:
            self.sync()

        # Restore file positions
        self._f_image.seek(image_pos)
        self._f_dest.seek(dest_pos)
        if self._f_bmap:
            self._f_bmap.seek(bmap_pos)

    def sync(self):
        """ Synchronize the destination file to make sure all the data are
        actually written to the disk. """

        try:
            os.fsync(self._f_dest.fileno()),
        except OSError as err:
            raise Error("cannot synchronize '%s': %s " \
                        % (self._dest_path, err.strerror))


class BmapBdevCopy(BmapCopy):
    """ This class is a specialized version of 'BmapCopy' which copies the
    image to a block device. Unlike the base 'BmapCopy' class, this class does
    various optimizations specific to block devices, e.g., switchint to the
    'noop' I/O scheduler. """

    def _open_destination_file(self):
        """ Open the block device in exclusive mode. """

        try:
            self._f_dest = os.open(self._dest_path, os.O_WRONLY | os.O_EXCL)
        except OSError as err:
            raise Error("cannot open block device '%s' in exclusive mode: %s" \
                        % (self._dest_path, err.strerror))

        try:
            os.fstat(self._f_dest).st_mode
        except OSError as err:
            raise Error("cannot access block device '%s': %s" \
                        % (self._dest_path, err.strerror))

        # Turn the block device file descriptor into a file object
        try:
            self._f_dest = os.fdopen(self._f_dest, "wb")
        except OSError as err:
            os.close(self._f_dest)
            raise Error("cannot open block device '%s': %s" \
                        % (self._dest_path, err))

        self._f_dest_needs_close = True

    def _tune_block_device(self):
        """" Tune the block device for better performance:
        1. Switch to the 'noop' I/O scheduler if it is available - sequential
           write to the block device becomes a lot faster comparing to CFQ.
        2. Limit the write buffering - we do not need the kernel to buffer a
           lot of the data we send to the block device, because we write
           sequentially. Limit the buffering.

        The old settings are saved in order to be able to restore them later.
        """
        # Switch to the 'noop' I/O scheduler
        try:
            with open(self._sysfs_scheduler_path, "r+") as f_scheduler:
                contents = f_scheduler.read()
                f_scheduler.seek(0)
                f_scheduler.write("noop")
        except IOError:
            # No problem, this is just an optimization.
            return

        # The file contains a list of scheduler with the current
        # scheduler in square brackets, e.g., "noop deadline [cfq]".
        # Fetch the current scheduler name
        import re

        match = re.match(r'.*\[(.+)\].*', contents)
        self.old_scheduler_value = match.group(1)

        # Limit the write buffering
        try:
            with open(self._sysfs_max_ratio_path, "r+") as f_ratio:
                self.old_max_ratio_value = f_ratio.read()
                f_ratio.seek(0)
                f_ratio.write("1")
        except IOError:
            return

    def _restore_bdev_settings(self):
        """ Restore old block device settings which we changed in
        '_tune_block_device()'. """

        if self.old_scheduler_value is not None:
            try:
                with open(self._sysfs_scheduler_path, "w") as f_scheduler:
                    f_scheduler.write(self.old_scheduler_value)
            except IOError:
                # No problem, this is just an optimization.
                return

        if self.old_max_ratio_value is not None:
            try:
                with open(self._sysfs_max_ratio_path, "w") as f_ratio:
                    f_ratio.write(self.old_max_ratio_value)
            except IOError:
                return

    def copy(self, sync = True, verify = True):
        """ The same as in the base class but tunes the block device for better
        performance before starting writing. Additionally, it forces block
        device synchronization from time to time in order to make sure we do
        not get stuck in 'fsync()' for too long time. The problem is that the
        kernel synchronizes block devices when the file is closed. And the
        result is that if the user interrupts us while we are copying the data,
        the program will be blocked in 'close()' waiting for the block device
        synchronization, which may last minutes for slow USB stick. This is
        very bad user experience, and we work around this effect by
        synchronizing from time to time. """

        try:
            self._tune_block_device()
            BmapCopy.copy(self, sync, verify)
        except:
            self._restore_bdev_settings()
            raise

    def __init__(self, image, dest, bmap = None):
        """ The same as the constructur of the 'BmapCopy' base class, but adds
        useful guard-checks specific to block devices. """

        # Call the base class construcor first
        BmapCopy.__init__(self, image, dest, bmap)

        self._batch_bytes = 1024 * 1024
        self._batch_blocks = self._batch_bytes / self.block_size
        self._batch_queue_len = 6
        self._dest_fsync_watermark = (6 * 1024 * 1024) / self.block_size

        self._sysfs_base = None
        self._sysfs_scheduler_path = None
        self._sysfs_max_ratio_path = None
        self.old_scheduler_value = None
        self.old_max_ratio_value = None

        # If the image size is known (i.e., it is not compressed) - check that
        # itfits the block device.
        if self.image_size:
            try:
                bdev_size = os.lseek(self._f_dest.fileno(), 0, os.SEEK_END)
                os.lseek(self._f_dest.fileno(), 0, os.SEEK_SET)
            except OSError as err:
                raise Error("cannot seed block device '%s': %s " \
                            % (self._dest_path, err.strerror))

            if bdev_size < self.image_size:
                raise Error("the image file '%s' has size %s and it will not " \
                            "fit the block device '%s' which has %s capacity" \
                            % (self._image_path, self.image_size_human,
                               self._dest_path, human_size(bdev_size)))

        # Construct the path to the sysfs directory of our block device
        st_rdev = os.fstat(self._f_dest.fileno()).st_rdev
        self._sysfs_base = "/sys/dev/block/%s:%s/" \
                           % (os.major(st_rdev), os.minor(st_rdev))

        # Check if the 'queue' sub-directory exists. If yes, then our block
        # device is entire disk. Otherwise, it is a partition, in which case we
        # need to go one level up in the sysfs hierarchy.
        try:
            if not os.path.exists(self._sysfs_base + "queue"):
                self._sysfs_base = self._sysfs_base + "../"
        except OSError:
            # No problem, this is just an optimization.
            pass

        self._sysfs_scheduler_path = self._sysfs_base + "queue/scheduler"
        self._sysfs_max_ratio_path = self._sysfs_base + "bdi/max_ratio"
