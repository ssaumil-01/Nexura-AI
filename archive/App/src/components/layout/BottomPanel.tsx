import React from 'react';
import ChatPanel from '../chat/ChatPanel';

const BottomPanel: React.FC = () => {
  return (
    <footer className="bottom-panel">
      <ChatPanel />
    </footer>
  );
};

export default BottomPanel;