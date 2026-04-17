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

  // Sort files by path length to ensure parents are created before children (though not strictly necessary with this map approach)
  const sortedFiles = [...flatFiles].sort((a, b) => a.path.length - b.path.length);

  sortedFiles.forEach(file => {
    const newNode: TreeNode = {
      name: file.name,
      path: file.path,
      is_dir: file.is_dir,
      children: []
    };
    
    map[file.path] = newNode;

    // Determine the parent path
    // We assume the parent path is everything before the last separator
    const pathParts = file.path.split(/[\\/]/);
    pathParts.pop();
    const parentPath = pathParts.join('/');

    // If the parent is the root or not in our map (e.g. at the top level), add to root
    if (parentPath === rootPath || !map[parentPath] || parentPath === rootPath.replace(/[\\/]$/, '')) {
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
