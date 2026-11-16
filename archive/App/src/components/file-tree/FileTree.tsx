import React, { useMemo } from 'react';
import FileItem from './FileItem';
import { useAppStore } from '../../store/useAppStore';
import { buildFileTree } from '../../utils/treeConverter';

const FileTree: React.FC = () => {
  const files = useAppStore((state) => state.files);
  const selectedFolder = useAppStore((state) => state.selectedFolder);

  const tree = useMemo(() => {
    if (!selectedFolder || files.length === 0) return [];
    return buildFileTree(selectedFolder, files);
  }, [selectedFolder, files]);

  if (tree.length === 0) {
    return <div className="tree-placeholder">No files found</div>;
  }

  return (
    <div className="file-tree">
      {tree.map((node) => (
        <FileItem 
          key={node.path} 
          node={node} 
          depth={0}
        />
      ))}
    </div>
  );
};

export default FileTree;
