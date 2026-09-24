"""Permission baseline for explicitly selected, test-owned fixture entries."""
import stat


def remove_shared_write(path):
    """Preserve all other mode bits; never chmod symlinks or shared file inodes."""
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        return
    # Directory link counts include child directories, not external hardlinks.
    if not stat.S_ISDIR(metadata.st_mode) and metadata.st_nlink > 1:
        return
    path.chmod(stat.S_IMODE(metadata.st_mode) & ~0o022)


def normalize_positive_fixture_tree(root):
    """Secure a test-owned baseline before reads or deliberate damage, without following links."""
    remove_shared_write(root)
    if stat.S_ISDIR(root.lstat().st_mode):
        for child in root.iterdir():
            normalize_positive_fixture_tree(child)
