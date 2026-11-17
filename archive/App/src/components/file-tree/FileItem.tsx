import React, { useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { useAppStore } from '../../store/useAppStore';
import { TreeNode } from '../../utils/treeConverter';
import { FiChevronRight, FiChevronDown } from "react-icons/fi";
import { getIconForFile, getIconForFolder } from 'vscode-icons-js';

interface FileItemProps {
  node: TreeNode;
  depth: number;
}

const ICON_BASE_URL = 'https://raw.githubusercontent.com/vscode-icons/vscode-icons/master/icons';

const FileItem: React.FC<FileItemProps> = ({ node, depth }) => {
  const [isExpanded, setIsExpanded] = useState(false);
  const setActiveFile = useAppStore((state) => state.setActiveFile);
  const addOpenFile = useAppStore((state) => state.addOpenFile);
  const activeFile = useAppStore((state) => state.activeFile);
  const setFileContent = useAppStore((state) => state.setFileContent);
  const fileContents = useAppStore((state) => state.fileContents);
  
  const isActive = activeFile === node.path;

  const handleClick = async (e: React.MouseEvent) => {
    e.stopPropagation();
    
    if (node.is_dir) {
      setIsExpanded(!isExpanded);
      return;
    }

    try {
      addOpenFile(node.path);
      setActiveFile(node.path);
      
      // Only fetch if not already in cache
      if (!(node.path in fileContents)) {
        const content: string = await invoke('read_file', { path: node.path });
        setFileContent(node.path, content);
      }
    } catch (err) {
      console.error('Failed to read file:', err);
      setFileContent(node.path, `Error loading file: ${err}`);
    }
  };

  const getFileIcon = () => {
    if (node.is_dir) {
      const folderIcon = getIconForFolder(node.name);
      return `${ICON_BASE_URL}/${folderIcon}`;
    }
    const fileIcon = getIconForFile(node.name);
    return `${ICON_BASE_URL}/${fileIcon}`;
  };

  return (
    <div className="tree-node-container">
      <div 
        className={`file-item ${isActive ? 'active' : ''} ${node.is_dir ? 'directory' : 'file'}`}
        style={{ paddingLeft: `${depth * 16 + 12}px` }}
        onClick={handleClick}
      >
        <span className="expand-icon">
          {node.is_dir ? (
            isExpanded ? <FiChevronDown /> : <FiChevronRight />
          ) : null}
        </span>
        <img 
          src={getFileIcon()} 
          alt="" 
          className="file-type-icon"
          onError={(e) => {
            // Fallback to simple icon if VSCode icon fails to load
            (e.target as HTMLImageElement).style.display = 'none';
          }}
        />
        <span className="file-name">{node.name}</span>
      </div>

      {node.is_dir && isExpanded && node.children.length > 0 && (
        <div className="node-children">
          {node.children.map((child) => (
            <FileItem 
              key={child.path} 
              node={child} 
              depth={depth + 1} 
            />
          ))}
        </div>
      )}
      
      {node.is_dir && isExpanded && node.children.length === 0 && (
        <div 
          className="tree-placeholder" 
          style={{ paddingLeft: `${(depth + 1) * 16 + 24}px`, textAlign: 'left', marginTop: 0 }}
        >
          (empty)
        </div>
      )}
    </div>
  );
};

export default FileItem;
