import React, { useState, useRef, useEffect } from 'react';
import { VscSend, VscRobot, VscPerson } from 'react-icons/vsc';

interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
}

const ChatPanel: React.FC = () => {
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<Message[]>([
    { id: '1', role: 'assistant', content: 'Hello! I am your Nexura-AI coding assistant. How can I help you today?' },
  ]);
  
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const handleSend = () => {
    if (!input.trim()) return;

    const userMessage: Message = {
      id: Date.now().toString(),
      role: 'user',
      content: input,
    };

    setMessages((prev) => [...prev, userMessage]);
    setInput('');

    // Simulate AI response after a short delay
    setTimeout(() => {
      const assistantMessage: Message = {
        id: (Date.now() + 1).toString(),
        role: 'assistant',
        content: `I've received your message: "${input}". Since this is a UI demo, I'm just acknowledging it!`,
      };
      setMessages((prev) => [...prev, assistantMessage]);
    }, 1000);
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="chat-panel">
      <div className="chat-messages">
        {messages.map((msg) => (
          <div key={msg.id} className={`chat-message-wrapper ${msg.role}`}>
            <div className="message-header">
              <div className={`message-avatar ${msg.role}`}>
                {msg.role === 'assistant' ? <VscRobot /> : <VscPerson />}
              </div>
              <span className="message-role">{msg.role === 'assistant' ? 'Nexura AI' : 'You'}</span>
            </div>
            <div className={`chat-message ${msg.role}`}>
              {msg.content}
            </div>
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>
      
      <div className="chat-input-container">
        <input 
          type="text" 
          className="chat-input" 
          placeholder="Ask Nexura AI anything..." 
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyPress}
        />
        <button 
          className="chat-send-button" 
          onClick={handleSend}
          disabled={!input.trim()}
          title="Send message"
        >
          <VscSend size={18} />
        </button>
      </div>
    </div>
  );
};

export default ChatPanel;
