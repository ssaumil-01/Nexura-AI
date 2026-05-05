import { FileEntry } from '../store/useAppStore';

export interface TreeNode {
  name: string;
  path: string;
  is_dir: boolean;
  children: TreeNode[];
}

/**
 * Converts a flat list of FileEntry objects (absolute paths) into a nested TreeNode structure.
 * It uses the rootPath to calculate relative positions.
 */
export const buildFileTree = (rootPath: string, flatFiles: FileEntry[]): TreeNode[] => {
  const root: TreeNode[] = [];
  const map: { [path: string]: TreeNode } = {};

  // Normalize rootPath to use forward slashes
  const normalizedRootPath = rootPath.replace(/\\/g, '/').replace(/\/$/, '');

  // Sort files by path length to ensure parents are created before children
  const sortedFiles = [...flatFiles].sort((a, b) => a.path.length - b.path.length);

  sortedFiles.forEach(file => {
    const normalizedFilePath = file.path.replace(/\\/g, '/');
    const newNode: TreeNode = {
      name: file.name,
      path: file.path, // keep original path for backend calls
      is_dir: file.is_dir,
      children: []
    };
    
    map[normalizedFilePath] = newNode;

    // Determine the parent path
    const pathParts = normalizedFilePath.split('/');
    pathParts.pop();
    const parentPath = pathParts.join('/');

    // If the parent is the root or not in our map (e.g. at the top level), add to root
    if (parentPath === normalizedRootPath || !map[parentPath] || parentPath === '') {
      root.push(newNode);
    } else {
      map[parentPath].children.push(newNode);
    }
  });

  // Sort each level: directories first, then alphabetical
  const sortTree = (nodes: TreeNode[]) => {
    nodes.sort((a, b) => {
      if (a.is_dir !== b.is_dir) return b.is_dir ? 1 : -1;
      return a.name.localeCompare(b.name);
    });
    nodes.forEach(node => {
      if (node.children.length > 0) sortTree(node.children);
    });
  };

  sortTree(root);
  return root;
};
